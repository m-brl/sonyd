"""Logique métier : construit les requêtes et interprète les réponses du casque.

Ce module ne connaît rien de la connexion : chaque fonction reçoit un `device` qui
expose `await device.request(payload, reply_code, timeout)` (voir daemon.Device).

Codes repris de Gadgetbridge (PayloadTypeV1/V2) ; le format ANC 0x19 du XM6 a été
déterminé expérimentalement.
"""

from dataclasses import dataclass

from .daemon import ConnectionLost

FW_VERSION_REQUEST, FW_VERSION_REPLY = 0x04, 0x05
BATTERY_REQUEST, BATTERY_REPLY, BATTERY_NOTIFY = 0x22, 0x23, 0x25
POWER_SET = 0x24
AUTO_POWER_OFF_GET, AUTO_POWER_OFF_RET = 0x26, 0x27
ANC_GET, ANC_RET, ANC_SET, ANC_NOTIFY = 0x66, 0x67, 0x68, 0x69
BUTTON_MODE_GET, BUTTON_MODE_RET, BUTTON_MODE_SET, BUTTON_MODE_NOTIFY = 0xF6, 0xF7, 0xF8, 0xF9

# Réglages on/off : nom -> code fonction, octets ajoutés après la valeur dans le SET,
# et requêtes GET (requête, code de réponse) à essayer dans l'ordre.
# Gadgetbridge lit la pause avec 26 01 mais la règle avec f8 01 : on tente aussi f6 01.
# Valeur inversée sur le casque : 00 = activé, 01 = désactivé.
TOGGLES = {
    "pause": (0x01, b"", ((AUTO_POWER_OFF_GET, AUTO_POWER_OFF_RET), (BUTTON_MODE_GET, BUTTON_MODE_RET))),
    "speak-to-chat": (0x0C, b"\x01", ((BUTTON_MODE_GET, BUTTON_MODE_RET),)),
}

# Codes fonction du contrôle du son ambiant : XM6, XM5, anciens modèles.
# L'ordre compte : le XM6 répond aussi à 0x17, mais avec des zéros.
ANC_FEATURES = (0x19, 0x17, 0x15)

MODES = ("off", "nc", "ambient")
ANC_CYCLE = ("nc", "ambient", "off")  # ordre de `anc switch`
AMBIENT_LEVEL_MAX = 20


class RequestError(Exception):
    pass


@dataclass
class Battery:
    level: int
    charging: bool

    def __str__(self):
        return f"{self.level}%" + (" (charging)" if self.charging else "")


@dataclass
class AncState:
    mode: str
    voice: bool
    level: int
    raw: bytes  # payload brut : sert de gabarit pour ANC_SET

    def __str__(self):
        return f"mode={self.mode} voice={'on' if self.voice else 'off'} level={self.level}"


# --- Interprétation -------------------------------------------------------


def parse_battery(p: bytes) -> Battery:
    if len(p) < 4:
        raise RequestError(f"invalid battery reply: {p.hex(' ')}")
    return Battery(level=p[2], charging=p[3] == 1)


def parse_anc(p: bytes) -> AncState:
    """XM6 (0x19) : code 19 01 on ambiant voix niveau ?? ??
    XM5 (0x17) / anciens (0x15) : voix et niveau sont les deux derniers octets."""
    feature = p[1] if len(p) > 1 else None
    if feature == 0x19 and len(p) == 9:
        voice_i, level_i = 5, 6
    elif feature in (0x15, 0x17) and 6 <= len(p) <= 8:
        voice_i, level_i = len(p) - 2, len(p) - 1
    else:
        raise RequestError(f"unknown ANC format: {p.hex(' ')}")

    if p[3] == 0x00:
        mode = "off"
    elif feature == 0x17 and len(p) == 8 and p[5] in (0x03, 0x05):
        mode = "wind"
    elif p[4] == 0x01:
        mode = "ambient"
    else:
        mode = "nc"
    return AncState(mode=mode, voice=bool(p[voice_i]), level=p[level_i], raw=bytes(p))


def build_anc_set(current: AncState, mode: str, voice: bool, level: int) -> bytes:
    """Construit un ANC_SET au même format (code fonction, longueur) que l'état lu."""
    template, feature = current.raw, current.raw[1]
    out = bytearray([ANC_SET, feature, 0x01, int(mode != "off"), int(mode == "ambient")])
    if feature == 0x17 and len(template) == 8:
        out.append(0x02)  # pas de réduction du bruit du vent
    out += bytes([int(voice), level])
    if feature == 0x19:
        out += template[7:]  # octets de rôle inconnu, recopiés tels quels
    return bytes(out)


def parse_toggle(p: bytes, name: str) -> bool:
    feature = TOGGLES[name][0]
    if len(p) < 3 or p[1] != feature:
        raise RequestError(f"invalid {name} reply: {p.hex(' ')}")
    return p[2] == 0x00


def describe(p: bytes) -> str | None:
    """Description lisible d'une notification spontanée, ou None si inconnue."""
    try:
        if p[0] in (ANC_RET, ANC_NOTIFY):
            return f"anc: {parse_anc(p)}"
        if p[0] in (BATTERY_REPLY, BATTERY_NOTIFY):
            return f"battery: {parse_battery(p)}"
        if p[0] in (AUTO_POWER_OFF_RET, BUTTON_MODE_RET, BUTTON_MODE_NOTIFY):
            for name, (feature, _, _) in TOGGLES.items():
                if p[1] == feature:
                    return f"{name}: {'on' if parse_toggle(p, name) else 'off'}"
    except (RequestError, IndexError):
        pass
    return None


# --- Requêtes -------------------------------------------------------------


async def firmware(device) -> str:
    p = await device.request(bytes([FW_VERSION_REQUEST, 0x02]), FW_VERSION_REPLY)
    if len(p) < 3:
        raise RequestError(f"invalid firmware reply: {p.hex(' ')}")
    return p[3:3 + p[2]].decode(errors="replace")


async def battery(device) -> Battery:
    return parse_battery(await device.request(bytes([BATTERY_REQUEST, 0x00]), BATTERY_REPLY))


async def poweroff(device) -> str:
    """Éteint le casque. Il coupe le lien aussitôt, parfois avant même l'ACK."""
    try:
        await device.request(bytes([POWER_SET, 0x03, 0x01]))
    except ConnectionLost:
        pass  # coupure due à l'extinction : c'est le résultat attendu
    return "powered off"


async def get_toggle(device, name: str) -> bool:
    feature, _, gets = TOGGLES[name]
    for get, reply in gets:
        try:
            p = await device.request(bytes([get, feature]), reply, timeout=2)
        except TimeoutError:
            continue
        return parse_toggle(p, name)
    raise RequestError(f"headset did not answer any known {name} command")


async def set_toggle(device, name: str, on: bool) -> bool:
    feature, suffix, _ = TOGGLES[name]
    await device.request(bytes([BUTTON_MODE_SET, feature, 0x00 if on else 0x01]) + suffix)
    return await get_toggle(device, name)


async def status(device) -> AncState:
    for feature in ANC_FEATURES:
        try:
            p = await device.request(bytes([ANC_GET, feature]), ANC_RET, timeout=2)
        except TimeoutError:
            continue
        return parse_anc(p)
    raise RequestError("headset did not answer any known ANC command")


async def set_anc(device, mode: str, level: int | None = None, voice: bool | None = None) -> AncState:
    """Change le mode ; level et voice gardent leur valeur actuelle s'ils sont omis.
    En mode off, le casque ignore level et voice."""
    if mode not in MODES:
        raise RequestError(f"unknown mode '{mode}' (expected: {', '.join(MODES)})")
    if level is not None and not 0 <= level <= AMBIENT_LEVEL_MAX:
        raise RequestError(f"level must be between 0 and {AMBIENT_LEVEL_MAX}")
    return await _apply_anc(device, await status(device), mode, level, voice)


async def switch_anc(device) -> AncState:
    """Passe au mode suivant : nc → ambient → off → nc (wind compte comme nc)."""
    current = await status(device)
    mode = "nc" if current.mode == "wind" else current.mode
    next_mode = ANC_CYCLE[(ANC_CYCLE.index(mode) + 1) % len(ANC_CYCLE)]
    return await _apply_anc(device, current, next_mode)


async def _apply_anc(device, current: AncState, mode: str,
                     level: int | None = None, voice: bool | None = None) -> AncState:
    level = current.level if level is None else level
    voice = current.voice if voice is None else voice
    await device.request(build_anc_set(current, mode, voice, level))
    return await status(device)
