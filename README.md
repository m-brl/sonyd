# sonyd

Control a Sony WH-1000XM6 headset from a Linux PC (noise cancelling, ambient sound, battery…) without going through the phone app.

`sonyd` is a small Python daemon that talks to the headset over Bluetooth RFCOMM using Sony's proprietary protocol, and exposes a text command interface on a Unix socket. `sonyctl` is the matching command-line client.

```console
$ ./sonyctl.py status
mode=ambient voice=off level=20
$ ./sonyctl.py anc nc
mode=nc voice=off level=20
$ ./sonyctl.py battery
59% (charging)
```

## Requirements

- Linux with BlueZ (the RFCOMM socket comes from Python's standard library, so PyBluez is not needed)
- Python ≥ 3.11, standard library only
- The headset paired with the PC (`bluetoothctl pair <MAC>`)
- Optional: `sdptool` (from BlueZ), used to look up the RFCOMM channel. Without it, the daemon falls back to channel 9.

## Installation

### Nix

The repository is a flake that provides `packages.<system>.default` and `overlays.default`. The package installs the `sonyd` and `sonyctl` commands, with `sdptool` from BlueZ in their `PATH`.

```console
$ nix run github:m-brl/sonyd -- 58:18:62:17:D8:FE     # try it without installing
$ nix profile install .                                  # from a local checkout
```

In a NixOS or Home Manager configuration:

```nix
{
  inputs.sonyd.url = "github:m-brl/sonyd";

  outputs = { nixpkgs, sonyd, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        { nixpkgs.overlays = [ sonyd.overlays.default ]; }
        ({ pkgs, ... }: { environment.systemPackages = [ pkgs.sonyd ]; })
      ];
    };
  };
}
```

### From the repository

Without installing anything, run `python3 -m sonyd` and `./sonyctl.py` from the repository root. `pip install .` installs the `sonyd` and `sonyctl` commands.

## Usage

Start the daemon with the headset's Bluetooth address (`bluetoothctl devices`):

```console
$ python3 -m sonyd 58:18:62:17:D8:FE
INFO sonyd.ipc: listening on /run/user/1000/sonyd.sock
```

| Option | Description |
|---|---|
| `--keepalive SECONDS` | How long the connection stays open after the last request (default: 300) |
| `--channel N` | RFCOMM channel (default: SDP lookup, then 9) |
| `--socket PATH` | Unix socket path (default: `$XDG_RUNTIME_DIR/sonyd.sock`) |
| `-v`, `--verbose` | Log every frame exchanged with the headset |

Then send commands with the client:

| Command | Effect |
|---|---|
| `status` | Current ambient sound control state (also the default when no command is given) |
| `battery` | Battery level and charging state |
| `firmware` | Firmware version |
| `anc <off\|nc\|ambient> [level] [voice\|novoice]` | Change the mode. `level` (0–20) is the ambient sound level, and `voice` is focus on voice. Omitted values keep their current setting. |
| `anc switch` | Cycle to the next mode: `nc` → `ambient` → `off` → `nc`. Level and focus on voice are kept. Handy for a keyboard shortcut. |
| `pause [on\|off]` | Pause playback when the headset is taken off. Without an argument, show the current setting. |
| `speak-to-chat [on\|off]` | Speak-to-chat: switch to ambient sound and pause when you start talking. Without an argument, show the current setting. |
| `poweroff` | Switch the headset off. |
| `help` | List the commands |

```console
$ ./sonyctl.py anc ambient 15 voice
mode=ambient voice=on level=15
$ ./sonyctl.py anc off
mode=off voice=on level=15
$ ./sonyctl.py anc switch
mode=nc voice=on level=15
$ ./sonyctl.py speak-to-chat off
off
```

`sonyctl` exits with status 1 on error and prints the message on stderr, so it can be used in scripts, keyboard shortcuts or status bars.

Both the daemon and the client honour the `SONYD_SOCKET` environment variable, which is handy to run a test instance next to the main one.

## How it works

The connection is opened lazily on the first request and kept alive for `--keepalive` seconds after the last one. Each new request resets the timer. If the headset is switched off or drops the link, the pending request fails and the next one reconnects.

> **The headset accepts only one RFCOMM client at a time.** While the daemon holds the connection, the Sony app on the phone cannot connect (and vice versa: the daemon gets `Device or resource busy`).

### Architecture

```
sonyctl ──(Unix socket)──▶ ipc.py ──▶ request.py ──▶ daemon.py ──(RFCOMM)──▶ headset
```

| Module | Role |
|---|---|
| `sonyd/request.py` | Business logic: builds request payloads and parses replies. It knows nothing about the connection. It receives a `device` object and calls `device.request(payload, reply_code)`. |
| `sonyd/daemon.py` | Connection: RFCOMM socket, framing, ACKs, sequence numbers, keep-alive and reconnection. It does not interpret payloads. Unsolicited notifications from the headset go to an `on_notify` callback. |
| `sonyd/ipc.py` | Unix socket server. It translates each text line into a call to `request.py` and writes back the result. |
| `sonyd/__main__.py` | Wires the three together, handles CLI options and signals (SIGTERM/SIGINT shut down cleanly). |
| `sonyctl.py` | Command-line client. |
| `poc.py` | Standalone proof of concept with no daemon, handy for protocol experiments. |

### IPC protocol

The protocol is one UTF-8 text line per request and one line per response, prefixed with `ok:` or `err:`. A client may send several requests on the same connection.

```console
$ echo status | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/sonyd.sock
ok: mode=off voice=off level=20
```

The socket is created with mode `0600`. On startup, a stale socket left by a crashed instance is removed. A second instance refuses to start if another one is already listening.

## Headset protocol

The protocol was reverse-engineered by [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) (`service/devices/sony/headphones`). On the WH-1000XM6 it runs over the RFCOMM service with UUID `956c7b26-d49a-4ba8-b03f-b17d393cb6e2` (channel 9).

Each message is a frame:

```
3E | type | seq | length (4 bytes, big-endian) | payload | checksum | 3C
```

- `type` is `0x0C`/`0x0E` for commands and `0x01` for ACKs.
- `checksum` is the sum of the bytes from `type` to the end of `payload`, modulo 256.
- `3E`, `3C` and `3D` inside the frame are escaped as `3D` followed by `byte & 0xEF`.
- Every command is acknowledged by an ACK carrying `1 - seq`. The sender then uses the sequence number from the ACK.

| Request | Payload | Reply |
|---|---|---|
| Init | `00 00` | `01 …` (8 bytes means protocol v2) |
| Firmware | `04 02` | `05 02 <len> <ascii>` |
| Battery | `22 00` | `23 00 <level> <charging>` |
| Ambient sound control: get | `66 19` | `67 19 …` |
| Ambient sound control: set | `68 19 01 <on> <ambient> <voice> <level> 00 00` | ACK, then `69 19 …` notification |
| Power off | `24 03 01` | ACK (the headset may drop the link first) |
| Pause when taken off: get | `26 01` (fallback `f6 01`) | `27 01 <value>` / `f7 01 <value>` |
| Pause when taken off: set | `f8 01 <value>` | ACK |
| Speak-to-chat: get | `f6 0c` | `f7 0c <value> …` |
| Speak-to-chat: set | `f8 0c <value> 01` | ACK |

For the on/off settings, `<value>` is inverted: `00` means enabled and `01` disabled. These codes come from Gadgetbridge and have not been checked against Sony's app traffic yet.

The **`0x19` ambient sound control format is specific to the XM6** and was found experimentally:

```
66/67/68/69 | 19 | 01 | on | ambient | voice | level (0-20) | ?? ??
```

- The XM5 uses `0x17` instead. The XM6 also answers `0x17`, but with zeros only, so `0x19` must be tried first.
- The last two bytes are always `00 00` so far, and their meaning is unknown. They are copied as-is when setting.
- In `off` mode, the headset ignores the `voice` and `level` values sent.

The headset also sends `0xC9` notifications that contain readable event names (`opChkBatt`, `keyPwrBtn`, `keyPwrBtnHold`…). They are logged but not decoded yet.

## Limitations and roadmap

- Only tested with the WH-1000XM6 (firmware 3.1.5). Other models may need different command codes.
- Not supported yet: equalizer, speak-to-chat settings (sensitivity, timeout), auto power off, multipoint, and the other settings of the Sony app. To find their codes, capture the phone's Bluetooth traffic (Android developer options: *Enable Bluetooth HCI snoop log*), open `btsnoop_hci.log` in Wireshark with the `btrfcomm` filter, and toggle the setting in the app.
- Planned: a `systemd --user` service, decoding the `0xC9` notifications to react to the headset buttons, and pushing state changes to clients.
