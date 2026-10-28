"""Connexion au casque : garde le lien RFCOMM ouvert.

La connexion est ouverte à la première requête puis maintenue KEEPALIVE secondes
après la dernière. Ce module gère les trames, les ACK et les numéros de séquence,
mais ne connaît pas le sens des payloads (voir request.py).

Format d'une trame : 3E | type | seq | longueur (4 o, big-endian) | payload | checksum | 3C
"""

import asyncio
import logging
import re
import socket
from collections.abc import Callable

log = logging.getLogger("sonyd.daemon")

KEEPALIVE = 300  # secondes de connexion maintenue après la dernière requête

SONY_UUID = "956c7b26-d49a-4ba8-b03f-b17d393cb6e2"
DEFAULT_CHANNEL = 9

START, END, ESC = 0x3E, 0x3C, 0x3D
ESC_MASK = 0xEF

ACK, COMMAND_1, COMMAND_2 = 0x01, 0x0C, 0x0E
INIT_REQUEST, INIT_REPLY = 0x00, 0x01


class DeviceError(Exception):
    pass


# --- Trames ---------------------------------------------------------------


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (START, END, ESC):
            out += bytes([ESC, b & ESC_MASK])
        else:
            out.append(b)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out = bytearray()
    it = iter(data)
    for b in it:
        out.append((next(it) | ~ESC_MASK & 0xFF) if b == ESC else b)
    return bytes(out)


def encode(mtype: int, seq: int, payload: bytes) -> bytes:
    body = bytes([mtype, seq]) + len(payload).to_bytes(4, "big") + payload
    return bytes([START]) + escape(body + bytes([sum(body) & 0xFF])) + bytes([END])


def decode(frame: bytes) -> tuple[int, int, bytes]:
    """frame = contenu entre START et END (encore échappé). Renvoie (type, seq, payload)."""
    raw = unescape(frame)
    if len(raw) < 7:
        raise ValueError(f"frame too short: {raw.hex(' ')}")
    body, checksum = raw[:-1], raw[-1]
    if sum(body) & 0xFF != checksum:
        raise ValueError(f"invalid checksum: {raw.hex(' ')}")
    length = int.from_bytes(body[2:6], "big")
    payload = body[6:]
    if len(payload) != length:
        raise ValueError(f"length mismatch ({length} != {len(payload)})")
    return body[0], body[1], payload


def extract_frames(buf: bytearray) -> list[bytes]:
    """Retire de buf les trames complètes et les renvoie (sans START/END)."""
    frames = []
    while (start := buf.find(START)) >= 0 and (end := buf.find(END, start + 1)) >= 0:
        frames.append(bytes(buf[start + 1:end]))
        del buf[:end + 1]
    return frames


async def find_channel(mac: str) -> int:
    """Cherche le canal RFCOMM du service Sony via SDP (canal 9 par défaut)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "sdptool", "browse", mac,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return DEFAULT_CHANNEL
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError:
        proc.kill()
        return DEFAULT_CHANNEL
    text = out.decode(errors="replace")
    idx = text.lower().find(SONY_UUID)
    m = re.search(r"Channel:\s*(\d+)", text[idx:]) if idx >= 0 else None
    return int(m.group(1)) if m else DEFAULT_CHANNEL


# --- Connexion ------------------------------------------------------------


class Device:
    def __init__(self, mac: str, channel: int | None = None, keepalive: float = KEEPALIVE,
                 on_notify: Callable[[bytes], None] | None = None):
        self.mac = mac
        self.channel = channel
        self.keepalive = keepalive
        self.on_notify = on_notify or (lambda payload: None)
        self._lock = asyncio.Lock()  # une seule commande en vol à la fois
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._seq = 0
        self._ack: asyncio.Future | None = None  # ACK de la commande en cours
        self._reply: tuple[int, asyncio.Future] | None = None  # (code attendu, réponse)

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def request(self, payload: bytes, reply_code: int | None = None,
                      timeout: float = 3.0) -> bytes | None:
        """Envoie une commande (en se connectant si besoin), attend son ACK puis,
        si reply_code est donné, la réponse dont le premier octet vaut reply_code."""
        async with self._lock:
            self._cancel_idle()
            try:
                if not self.connected:
                    await self._connect()
                return await self._exchange(payload, reply_code, timeout)
            finally:
                if self.connected:
                    self._idle_task = asyncio.create_task(self._idle())

    async def disconnect(self):
        self._cancel_idle()
        async with self._lock:
            await self._close()

    # -- interne --

    async def _connect(self):
        if self.channel is None:
            self.channel = await find_channel(self.mac)
        log.info("connecting to %s (channel %d)...", self.mac, self.channel)
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.setblocking(False)
        try:
            async with asyncio.timeout(10):
                await asyncio.get_running_loop().sock_connect(sock, (self.mac, self.channel))
        except (OSError, TimeoutError) as e:
            sock.close()
            raise DeviceError(f"cannot connect to {self.mac}: {e or 'timeout'}") from e

        reader, self._writer = await asyncio.open_connection(sock=sock)
        self._seq = 0
        self._read_task = asyncio.create_task(self._read_loop(reader))
        try:
            await self._exchange(bytes([INIT_REQUEST, 0x00]), INIT_REPLY, 3.0)
        except BaseException:
            await self._close()
            raise
        log.info("connected")

    async def _exchange(self, payload: bytes, reply_code: int | None, timeout: float) -> bytes | None:
        loop = asyncio.get_running_loop()
        self._ack = loop.create_future()
        self._reply = (reply_code, loop.create_future()) if reply_code is not None else None
        log.debug("> %s", payload.hex(" "))
        try:
            self._writer.write(encode(COMMAND_1, self._seq, payload))
            async with asyncio.timeout(timeout):
                await self._writer.drain()
                await self._ack
                return (await self._reply[1]) if self._reply else None
        except TimeoutError:
            raise TimeoutError(f"no reply from headset to {payload.hex(' ')}") from None
        finally:
            self._ack = self._reply = None

    async def _read_loop(self, reader: asyncio.StreamReader):
        buf = bytearray()
        try:
            while chunk := await reader.read(1024):
                buf += chunk
                for frame in extract_frames(buf):
                    try:
                        self._handle(*decode(frame))
                    except ValueError as e:
                        log.warning("dropped frame: %s", e)
            log.info("connection closed by headset")
        except OSError as e:
            log.warning("connection lost: %s", e)
        finally:
            self._fail_pending(DeviceError("connection to headset lost"))
            self._teardown()

    def _handle(self, mtype: int, seq: int, payload: bytes):
        if mtype == ACK:
            self._seq = seq
            if self._ack and not self._ack.done():
                self._ack.set_result(None)
        elif mtype in (COMMAND_1, COMMAND_2):
            self._writer.write(encode(ACK, 1 - seq, b""))
            log.debug("< %s", payload.hex(" "))
            if self._reply and payload[:1] == bytes([self._reply[0]]) and not self._reply[1].done():
                self._reply[1].set_result(payload)
                return
            try:
                self.on_notify(payload)
            except Exception:
                log.exception("error in on_notify")
        else:
            log.debug("unknown frame type 0x%02x: %s", mtype, payload.hex(" "))

    def _fail_pending(self, exc: Exception):
        for fut in (self._ack, self._reply and self._reply[1]):
            if fut and not fut.done():
                fut.set_exception(exc)

    def _teardown(self):
        if self._writer:
            self._writer.close()
            self._writer = None

    async def _close(self):
        was_connected = self.connected
        task, self._read_task = self._read_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.wait([task])
        self._teardown()
        if was_connected:
            log.info("disconnected")

    async def _idle(self):
        await asyncio.sleep(self.keepalive)
        async with self._lock:
            self._idle_task = None
            log.info("no request for %ss, closing connection", self.keepalive)
            await self._close()

    def _cancel_idle(self):
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
