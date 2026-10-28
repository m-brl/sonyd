"""sonyd client: sonyctl [status | battery | firmware | anc <mode> [level] [voice|novoice]]"""

import socket
import sys

from .ipc import default_socket_path


def main():
    line = " ".join(sys.argv[1:]) or "status"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(default_socket_path())
            s.sendall(line.encode() + b"\n")
            response = s.makefile(encoding="utf-8").readline().strip()
    except OSError as e:
        sys.exit(f"cannot reach sonyd: {e}")

    status, _, message = response.partition(": ")
    if status != "ok":
        sys.exit(message or response)
    print(message)


if __name__ == "__main__":
    main()
