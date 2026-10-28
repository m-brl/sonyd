"""Point d'entrée : python3 -m sonyd 58:18:62:17:D8:FE"""

import argparse
import asyncio
import logging
import signal
import sys

from . import request
from .daemon import KEEPALIVE, Device
from .ipc import AlreadyRunning, IpcServer, default_socket_path

log = logging.getLogger("sonyd")


def log_notification(payload: bytes):
    log.info("notification %s", request.describe(payload) or payload.hex(" "))


async def run(device: Device, server: IpcServer):
    # SIGTERM (systemd) et SIGINT (Ctrl-C) : arrêt propre, socket supprimé et casque déconnecté
    main_task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, main_task.cancel)
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        log.info("shutting down")
    finally:
        await device.disconnect()


def main():
    ap = argparse.ArgumentParser(prog="sonyd", description="Sony headphones control daemon")
    ap.add_argument("mac", help="headset Bluetooth address")
    ap.add_argument("--channel", type=int, help="RFCOMM channel (default: SDP lookup)")
    ap.add_argument("--keepalive", type=float, default=KEEPALIVE,
                    help=f"seconds to keep the connection open after the last request (default: {KEEPALIVE})")
    ap.add_argument("--socket", default=default_socket_path(), help="Unix socket path")
    ap.add_argument("-v", "--verbose", action="store_true", help="log exchanged frames")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    device = Device(args.mac, channel=args.channel, keepalive=args.keepalive,
                    on_notify=log_notification)
    try:
        asyncio.run(run(device, IpcServer(device, args.socket)))
    except AlreadyRunning as e:
        sys.exit(f"sonyd: {e}")


if __name__ == "__main__":
    main()
