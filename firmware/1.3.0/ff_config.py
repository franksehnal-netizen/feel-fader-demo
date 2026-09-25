# ff_config.py — čistá config logika Feel Fader (bez hardware importů).
# Importováno z code.py (na zařízení) i z host pytestu (na PC).

DEFAULT_UACC = [
    1, 2, 3, 4, 5, 20, 21, 22, 23, 26, 31, 32,
    40, 41, 42, 43, 44, 45, 46, 47, 49, 50, 52,
    70, 71, 72, 73, 74, 75, 90, 91, 100, 101, 110, 112
]

DEFAULT_PRESETS = {
    "banks": [
        {"fader_cc": [11, 1],  "fader_ch": [0, 0], "encoder": 32, "encoder_ch": 0, "uacc_values": DEFAULT_UACC},
        {"fader_cc": [12, 13], "fader_ch": [1, 1], "encoder": 32, "encoder_ch": 1, "uacc_values": DEFAULT_UACC},
        {"fader_cc": [21, 22], "fader_ch": [2, 2], "encoder": 32, "encoder_ch": 2, "uacc_values": DEFAULT_UACC},
    ]
}

# Global fader-output profiles. Only these stable names cross the serial
# protocol; firmware maps them to the low-level ADC/interpolation parameters.
FADER_RESPONSE_DEFAULT = "balanced"
FADER_RESPONSE_PRESETS = ("immediate", "balanced", "smooth")


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def normalize_fader_response(value):
    """Return a safe, forward-compatible global fader response profile."""
    return value if value in FADER_RESPONSE_PRESETS else FADER_RESPONSE_DEFAULT


NAV_DEFAULT_CW = 0x52   # UP_ARROW (USB HID usage) — CW = po směru = nahoru
NAV_DEFAULT_CCW = 0x51  # DOWN_ARROW — CCW = proti směru = dolů


META_NAME_MAX  = 24
META_ICON_MAX  = 16
META_LABEL_MAX = 12


def _meta_sanitize(m):
    """Ořeže meta dict na limity; vrátí {} pro nevalidní/prázdný vstup."""
    if not isinstance(m, dict):
        return {}
    out = {}
    n = str(m.get("n") or "")[:META_NAME_MAX]
    i = str(m.get("i") or "")[:META_ICON_MAX]
    l_raw = m.get("l") or []
    l = [str(l_raw[j])[:META_LABEL_MAX] if (j < len(l_raw) and l_raw[j]) else "" for j in (0, 1)]
    if n: out["n"] = n
    if i: out["i"] = i
    if l[0] or l[1]: out["l"] = l
    return out


def _meta_from_web(b):
    """Vytáhne prezentační pole z web bank dictu → interní meta."""
    f1 = b.get("fader1") or {}
    f2 = b.get("fader2") or {}
    return _meta_sanitize({
        "n": b.get("name"), "i": b.get("icon"),
        "l": [f1.get("label"), f2.get("label")],
    })


def _nav_keys(raw, default_code):
    keys = [int(v) for v in (raw or []) if 0 <= int(v) <= 255]
    if not keys:
        keys = [default_code]
    return keys


def parse_macro_keys(raw):
    """Long-press makro: plochý list HID usage IDů (0..255). Prázdné = neaktivní."""
    return [int(v) for v in (raw or []) if 0 <= int(v) <= 255]


def active_macro_keys(macro_global, macro_keys, bank):
    """Které makro odpálit při long-pressu v dané bance.

    Prázdný per-bank seznam znamená "žádná akce", NE fallback na globální —
    jinak by nešlo makro pro jednu banku vypnout (spec 2026-08-08 §D).
    """
    if macro_global:
        return macro_keys or []
    return parse_macro_keys((bank or {}).get("macro_keys"))


def _normalize_bank_core(f1cc, f1ch, f2cc, f2ch, enc_cc, enc_ch, raw, meta):
    """Shared clamp/normalize logic for parse_banks() (NVM/JSON internal
    format) and normalize_web_config() (per-control web format) — the two
    previously duplicated ~45 lines of identical logic for everything except
    how fader1/fader2/encoder cc+channel are read off the input (audit
    2026-07-20, A-5). Callers extract those 6 values in their own input
    shape; `raw` is the per-bank dict in whichever shape the caller has, used
    only for fields whose key names are already identical in both formats
    (uacc_values, roller_mode, ks_notes, ks_channel, ks_velocity,
    nav_keys_cw/ccw, nav_invert). `meta` is pre-extracted by the caller
    (_meta_sanitize vs _meta_from_web differ in where they read name/icon/
    label from). Returns the internal firmware bank dict.
    """
    uacc = sorted({int(v) for v in raw.get("uacc_values", DEFAULT_UACC) if 0 <= int(v) <= 127})
    if not uacc:
        uacc = list(DEFAULT_UACC)
    mode = raw.get("roller_mode", "cc")
    if mode not in ("cc", "keyswitch", "track_nav", "cc_relative"):
        mode = "cc"
    ks_notes = [int(v) for v in raw.get("ks_notes", []) if 0 <= int(v) <= 127]
    ks_channel = _clamp(int(raw.get("ks_channel", enc_ch)), 0, 15)
    ks_velocity = _clamp(int(raw.get("ks_velocity", 100)), 1, 127)
    nav_cw = _nav_keys(raw.get("nav_keys_cw"), NAV_DEFAULT_CW)
    nav_ccw = _nav_keys(raw.get("nav_keys_ccw"), NAV_DEFAULT_CCW)
    nav_invert = bool(raw.get("nav_invert", False))
    bank = {
        "fader_cc":    [_clamp(int(f1cc), 0, 127), _clamp(int(f2cc), 0, 127)],
        "fader_ch":    [_clamp(int(f1ch), 0, 15), _clamp(int(f2ch), 0, 15)],
        "encoder":     _clamp(int(enc_cc), 0, 127),
        "encoder_ch":  enc_ch,
        "uacc_values": uacc,
        "roller_mode": mode,
        "ks_notes":    ks_notes,
        "ks_channel":  ks_channel,
        "ks_velocity": ks_velocity,
        "nav_keys_cw":  nav_cw,
        "nav_keys_ccw": nav_ccw,
        "nav_invert":   nav_invert,
        "macro_keys":   parse_macro_keys(raw.get("macro_keys")),
    }
    if meta:
        bank["m"] = meta
    return bank


def parse_banks(data):
    """Parsuje banks ze slovníku (společná logika pro NVM i JSON soubor)."""
    if "banks" not in data or not isinstance(data["banks"], list):
        raise ValueError("missing banks")
    banks = []
    for b in data["banks"]:
        legacy_ch = _clamp(int(b.get("channel", 0)), 0, 15)  # fallback pro starý formát
        fcc = b.get("fader_cc", [11, 1])
        if not isinstance(fcc, list) or len(fcc) != 2:
            fcc = [11, 1]
        fch = b.get("fader_ch", [legacy_ch, legacy_ch])
        if not isinstance(fch, list) or len(fch) != 2:
            fch = [legacy_ch, legacy_ch]
        enc_ch = _clamp(int(b.get("encoder_ch", legacy_ch)), 0, 15)
        banks.append(_normalize_bank_core(
            fcc[0], fch[0], fcc[1], fch[1], b.get("encoder", 32), enc_ch,
            b, _meta_sanitize(b.get("m")),
        ))
    if not banks:
        raise ValueError("empty banks")
    return {
        "banks": banks,
        "macro_keys": parse_macro_keys(data.get("macro_keys")),
        # Chybějící macro_global = True: starý config měl jen globální makro.
        "macro_global": bool(data.get("macro_global", True)),
        "fader_response": normalize_fader_response(data.get("fader_response")),
    }


def normalize_web_config(web_cfg):
    """Konvertuje cfg formát webové appky na interní firmware banks.

    Web posílá per banku:
      { fader1:{cc,channel}, fader2:{cc,channel}, encoder:{cc,channel},
        uacc_values:[...], roller_mode, ks_notes:[...], ks_channel, ks_velocity }
    Vrací list firmware bank dictů, nebo None když je struktura neplatná.
    Pure (bez hardware) — společná logika pro SysEx i serial CMD_W, host-testovatelná.
    """
    if not isinstance(web_cfg.get("banks"), list) or len(web_cfg["banks"]) == 0:
        return None
    new_banks = []
    for b in web_cfg["banks"]:
        f1  = b.get("fader1",  {})
        f2  = b.get("fader2",  {})
        enc = b.get("encoder", {})
        enc_ch = _clamp(int(enc.get("channel", 0)), 0, 15)
        new_banks.append(_normalize_bank_core(
            f1.get("cc", 11), f1.get("channel", 0), f2.get("cc", 1), f2.get("channel", 0),
            enc.get("cc", 32), enc_ch, b, _meta_from_web(b),
        ))
    return new_banks


def merge_legacy_macro_state(web_cfg, new_banks, current_macro_global, current_banks):
    """Chrání pred tichou ztratou makra, kdyz CMD_W posle stary klient appky.

    Novy web appka VZDY posila top-level "macro_global" ve write payloadu;
    jeho absence je tedy spolehlivy signal "stary klient" (nezna Wave 2 —
    per-bank macro_keys ani global toggle). Bez tohoto merge by
    apply_web_config/apply_and_save_json tise resetovaly macro_global na
    True a kazdou banku na macro_keys: [] (audit finding 2, 2026-08-08),
    protoze normalize_web_config() uz dala kazde bance macro_keys: []
    (v jejim vstupu chybi klic).

    Kdyz "macro_global" v payloadu JE, chovej se presne jako drive (plny
    prepis). Kdyz chybi, zachovej aktualni macro_global zarizeni a per-bank
    macro_keys zarizeni (podle indexu banky); vse ostatni z new_banks
    (fadery/enkoder/roller mode/...) zustava z noveho payloadu beze zmeny.

    Vraci (macro_global, banks) — banks je novy list (puvodni new_banks se
    nemutuje).
    """
    if "macro_global" in web_cfg:
        return bool(web_cfg["macro_global"]), new_banks
    merged = []
    for i, b in enumerate(new_banks):
        nb = dict(b)
        if i < len(current_banks):
            nb["macro_keys"] = list(current_banks[i].get("macro_keys", []))
        merged.append(nb)
    return bool(current_macro_global), merged


def merge_legacy_fader_response(web_cfg, current_response):
    """Preserve the device setting when a pre-response-profile app writes config."""
    if "fader_response" not in web_cfg:
        return normalize_fader_response(current_response)
    return normalize_fader_response(web_cfg.get("fader_response"))


def due_noteoffs(pending, now_ms):
    """Rozdělí frontu pending note-offů na (due, zbytek) podle času.

    pending: list of (note, channel, due_ms). Vrací (due_list, rest_list).
    """
    due = [item for item in pending if item[2] <= now_ms]
    rest = [item for item in pending if item[2] > now_ms]
    return due, rest


# =========================
#  NVM FOOTER (HID flag) + INFO DICT
# =========================
FOOTER_SIZE   = 8        # vyhrazené bajty na KONCI NVM (oddělené od presetů na offsetu 0)
HID_FLAG_BIT  = 0x01
_FOOTER_MAGIC = (0xAE, 0x01)
_FOOTER_VERSION = 1


def crc8(data):
    """CRC-8 (poly 0x07), deterministická validace footeru."""
    crc = 0
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def pack_footer(flags, version=_FOOTER_VERSION):
    """Vrátí FOOTER_SIZE bajtů: magic(2) ver(1) flags(1) crc(1) pad(3)."""
    body = bytes([_FOOTER_MAGIC[0], _FOOTER_MAGIC[1], version & 0xFF, flags & 0xFF])
    return body + bytes([crc8(body), 0, 0, 0])


def unpack_footer(footer):
    """Validuje magic + CRC. Vrátí dict nebo None."""
    if footer is None or len(footer) < 5:
        return None
    if footer[0] != _FOOTER_MAGIC[0] or footer[1] != _FOOTER_MAGIC[1]:
        return None
    body = bytes([footer[0], footer[1], footer[2], footer[3]])
    if crc8(body) != footer[4]:
        return None
    flags = footer[3]
    return {"version": footer[2], "flags": flags, "hid_enabled": bool(flags & HID_FLAG_BIT)}


def build_info_dict(firmware, model, serial, hid_available=True, hid_enabled=False,
                    supports_14bit=False, supports_macros=False, schema_version=3,
                    config_hash=None, config_source=None, faders=None, bank=None,
                    fader_response=None, update=None):
    """Feature-discovery dict pro CMD_INFO (§10.7 + Wave 2 spec §2)."""
    info = {
        "firmware": firmware, "model": model, "schema_version": schema_version,
        "hid_available": bool(hid_available), "hid_enabled": bool(hid_enabled),
        "supports_14bit": bool(supports_14bit), "supports_macros": bool(supports_macros),
    }
    if serial:
        info["serial"] = serial
    if config_hash is not None:
        info["config_hash"] = config_hash
    if config_source is not None:
        info["config_source"] = config_source
    if faders is not None:
        info["faders"] = [int(faders[0]), int(faders[1])]
    if bank is not None:
        # Aktuální aktivní bank zařízení — umožní appce sync live-bank indikátoru
        # při (re)connectu bez čekání na Program Change (liveBank sync-on-load).
        info["bank"] = int(bank)
    if fader_response is not None:
        info["fader_response_presets"] = True
        info["fader_response"] = normalize_fader_response(fader_response)
    if update is not None:
        info["update"] = update
    return info


# =========================
#  WAVE 2 — KANONICKÁ SERIALIZACE + HASH
# =========================
import json as _json

try:
    import binascii as _binascii
    _HAS_CRC32 = True
except ImportError:
    _HAS_CRC32 = False


def _crc32_hash(data_bytes):
    """CRC32 (binascii) — primární. Volat jen když _HAS_CRC32."""
    return _binascii.crc32(data_bytes) & 0xFFFFFFFF


def _fnv1a_hash(data_bytes):
    """FNV-1a 32-bit — čistý Python fallback bez tabulek."""
    h = 2166136261
    for b in data_bytes:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def blob_checksum(data_bytes):
    """32-bit checksum pro NVM blob — stejný algoritmus jako state_hash."""
    return _crc32_hash(data_bytes) if _HAS_CRC32 else _fnv1a_hash(data_bytes)


_SPARSE_KEEP = ("uacc_values", "m")   # uacc: starý app při absenci nastaví [] (ne default)
                                       # m: má vlastní omit-if-empty pravidlo


def _bank_defaults(bank):
    """Defaulty shodné s parse_banks — pole s touto hodnotou lze vynechat."""
    return {
        "fader_cc": [11, 1], "fader_ch": [0, 0],
        "encoder": 32, "encoder_ch": 0,
        "roller_mode": "cc", "ks_notes": [],
        "ks_channel": bank.get("encoder_ch", 0),   # parse re-derivuje z enc_ch
        "ks_velocity": 100,
        "nav_keys_cw": [NAV_DEFAULT_CW], "nav_keys_ccw": [NAV_DEFAULT_CCW],
        "nav_invert": False,
        "macro_keys": [],
    }


def _sparse_bank(bank):
    defaults = _bank_defaults(bank)
    out = {}
    for k, v in bank.items():
        if k in _SPARSE_KEEP or k not in defaults or defaults[k] != v:
            out[k] = v
    return out


def serialize_state(banks, macro_keys, macro_global=True, fader_response=FADER_RESPONSE_DEFAULT):
    """JEDINÁ kanonická serializace presetů — používá ji save, CMD_R i hash.
    Sparse: pole rovná defaultům se vynechávají (spec §4); dict staví tento kód
    (fixní pořadí klíčů = pořadí v bank dictu), kompaktní separators."""
    state = {"banks": [_sparse_bank(b) for b in banks]}
    if macro_keys:
        state["macro_keys"] = macro_keys
    if not macro_global:
        state["macro_global"] = False   # True je default, vynechává se → hash starých configů se nemění
    response = normalize_fader_response(fader_response)
    if response != FADER_RESPONSE_DEFAULT:
        state["fader_response"] = response
    return _json.dumps(state, separators=(",", ":"))


def state_hash(state_str):
    """8 hex znaků nad kanonickým stringem. Pro app opaque token."""
    return "%08x" % blob_checksum(state_str.encode("utf-8"))


# =========================
#  WAVE 2 — NVM V2 BLOB (marker2 + len2 + crc32(4) + data)
# =========================
NVM_MARKER_V2 = b"\xFE\xEE"
NVM_MARKER_V1 = b"\xFE\xED"
NVM_V2_HEADER = 8   # 2 marker + 2 len + 4 checksum


def pack_presets_blob(data_bytes):
    n = len(data_bytes)
    c = blob_checksum(data_bytes)
    return (NVM_MARKER_V2
            + bytes([n & 0xFF, (n >> 8) & 0xFF])
            + bytes([(c >> s) & 0xFF for s in (0, 8, 16, 24)])
            + data_bytes)


def unpack_presets_blob(buf):
    """buf = bytes od offsetu 0 NVM. Vrátí data nebo None (marker/CRC fail)."""
    if buf is None or len(buf) < NVM_V2_HEADER or bytes(buf[0:2]) != NVM_MARKER_V2:
        return None
    n = buf[2] | (buf[3] << 8)
    if n <= 0 or NVM_V2_HEADER + n > len(buf):
        return None
    c = buf[4] | (buf[5] << 8) | (buf[6] << 16) | (buf[7] << 24)
    data = bytes(buf[NVM_V2_HEADER:NVM_V2_HEADER + n])
    return data if blob_checksum(data) == c else None


def unpack_presets_blob_v1(buf):
    """Legacy v1 formát (marker \xFE\xED + len2 + data, bez CRC) — jen migrace."""
    if buf is None or len(buf) < 4 or bytes(buf[0:2]) != NVM_MARKER_V1:
        return None
    n = buf[2] | (buf[3] << 8)
    if n <= 0 or 4 + n > len(buf):
        return None
    return bytes(buf[4:4 + n])


# =========================
#  WAVE 2 — SERIAL LINE PARSER (v2 rid framing + legacy)
# =========================
_SERIAL_CMDS = ("CMD_R", "CMD_INFO", "CMD_W", "CMD_HID",
                "CMD_UB", "CMD_UC", "CMD_UE", "CMD_UA")
_NO_PAYLOAD_V2 = ("CMD_R", "CMD_INFO", "CMD_UE", "CMD_UA")


def parse_serial_line(line):
    """Vrátí (cmd, rid, payload). rid=None značí legacy rámec (odpověď postaru).

    Legacy:  "CMD_R" / "CMD_INFO" / "CMD_W:{json}" / "CMD_HID:{json}"
    v2:      "CMD_R:<rid>" / "CMD_INFO:<rid>" / "CMD_W:<rid>:{json}" / "CMD_HID:<rid>:{json}" /
             "CMD_UB:<rid>:{json}" / "CMD_UC:<rid>:<name>:<offset>:<b64>" / "CMD_UE:<rid>" / "CMD_UA:<rid>"
    """
    if line in ("CMD_R", "CMD_INFO"):
        return (line, None, None)
    for cmd in _SERIAL_CMDS:
        prefix = cmd + ":"
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):]
        if cmd in ("CMD_W", "CMD_HID") and rest.startswith("{"):
            return (cmd, None, rest)            # legacy payload rámec
        if ":" in rest:
            rid, payload = rest.split(":", 1)
            return (cmd, rid, payload)          # v2 s payloadem
        if rest and cmd in _NO_PAYLOAD_V2:
            return (cmd, rest, None)            # v2 bez payloadu
        return (None, None, None)
    return (None, None, None)
