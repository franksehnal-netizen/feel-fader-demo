import time
import board
import analogio
import digitalio
import usb_midi
import usb_cdc
import adafruit_midi
import rotaryio
from adafruit_midi.control_change import ControlChange
from adafruit_midi.note_on import NoteOn
from adafruit_midi.note_off import NoteOff
from adafruit_midi.program_change import ProgramChange
from adafruit_midi.system_exclusive import SystemExclusive
import json
import microcontroller
import ff_config
from ff_config import DEFAULT_UACC, DEFAULT_PRESETS, parse_banks
import ff_update

import usb_hid
try:
    from adafruit_hid.keyboard import Keyboard
    _kbd = Keyboard(usb_hid.devices)   # vyhodí, pokud HID není povolené (prázdné devices)
except Exception:
    _kbd = None

# =========================
#  SETTINGS
# =========================
SEND_INITIAL_FADER_SNAPSHOT_ON_BOOT = False
SEND_INITIAL_ENCODER_SNAPSHOT_ON_BOOT = False
SEND_FADER_SNAPSHOT_ON_BANK_CHANGE   = True
SEND_ENCODER_SNAPSHOT_ON_BANK_CHANGE = True

# ADC filtrace / anti-jitter
SAMPLE_AVG    = 8
ARM_DELTA     = 4

# Fyzický rozsah faderů (7-bit, po inverzi) — remap na plných 0–127
FADER_RAW_MIN = 2    # hodnota při fyzickém dně faderu
FADER_RAW_MAX = 125  # hodnota při fyzickém vrcholu faderu

# Keyswitch note-off zpoždění (ms) — krátký gap, ať plugin zachytí událost
KEYSWITCH_NOTEOFF_MS = 20

# Track-nav: max kláves za jednu smyčku (ochrana proti zahlcení DAW při rychlém otočení)
NAV_MAX_BURST = 8

# Enkodér: 1 klik = delta 1 (pro PEC11R typicky 2)
ENCODER_DIVISOR = 2

# Presety (JSON na CIRCUITPY kořeni)
PRESETS_PATH = "/presets.json"

# Fader response profiles. "balanced" exactly preserves the former firmware
# behaviour; the app only stores the profile name, never these raw parameters.
FADER_RESPONSE_PROFILES = {
    "immediate": {"tolerance": 1, "hysteresis": 0, "rate_limit_ms": 8,
                  "interpolation": True, "interp_step_ms": 5, "interp_speed": 1.0, "interp_min_step": 1},
    "balanced":  {"tolerance": 2, "hysteresis": 1, "rate_limit_ms": 8,
                  "interpolation": True, "interp_step_ms": 5, "interp_speed": 0.35, "interp_min_step": 1},
    "smooth":    {"tolerance": 2, "hysteresis": 1, "rate_limit_ms": 8,
                  "interpolation": True, "interp_step_ms": 5, "interp_speed": 0.20, "interp_min_step": 1},
}

# =========================
#  SYSEX PROTOKOL
# =========================
MFR          = 0x7D   # non-commercial manufacturer ID (shodný s webovou appkou)
DEV_ID       = 0x01
CMD_W        = 0x01   # write config  (web → device nebo device → web)
CMD_R        = 0x02   # request config (web → device: "pošli mi svůj config")
CMD_INFO     = 0x03   # device info (SysEx)
CMD_CHUNK    = 0x04   # config chunk (device → web, odpověď na CMD_R)
CMD_ACK      = 0x05   # potvrzení CMD_W (device → web)
CMD_ERR      = 0x06   # chyba (device → web)
CMD_HID      = 0x07   # web → device: nastav HID flag {enabled} a rebootni
UPDATE_ROOT = "/"   # kořen FS zařízení; testy ho monkeypatchují na tmp
FIRMWARE_VER = ff_update.read_version(UPDATE_ROOT)
RUNNING_SLOT = ff_update.active_slot(UPDATE_ROOT)   # slot, ze kterého tento kód běží
MODEL_ID     = "FF"

# =========================
#  PRESETS LOADER (JSON)
# =========================
# DEFAULT_UACC, DEFAULT_PRESETS a parse_banks jsou v ff_config.py

# =========================
#  NVM PERSISTENCE
# =========================

def _nvm_load():
    """Načte bytes z NVM (legacy v1 formát), vrátí None pokud obsah neplatný."""
    try:
        nvm = microcontroller.nvm
        return ff_config.unpack_presets_blob_v1(bytes(nvm[0:len(nvm) - ff_config.FOOTER_SIZE]))
    except Exception:
        return None

def _nvm_save_v2(data_bytes):
    """Bezpečné pořadí zápisu: invalidovat marker → tělo → marker naposled.
    Vrací (ok, reason)."""
    try:
        nvm = microcontroller.nvm
        blob = ff_config.pack_presets_blob(data_bytes)
        if len(blob) > len(nvm) - ff_config.FOOTER_SIZE:
            return (False, "too_large")
        nvm[0:2] = b"\x00\x00"
        nvm[2:len(blob)] = blob[2:]
        nvm[0:2] = blob[0:2]
        return (True, None)
    except Exception:
        return (False, "nvm_write")


def _nvm_load_v2():
    """Vrátí data bytes z v2 blobu, nebo None."""
    try:
        nvm = microcontroller.nvm
        return ff_config.unpack_presets_blob(bytes(nvm[0:len(nvm) - ff_config.FOOTER_SIZE]))
    except Exception:
        return None

def _hid_flag_read():
    """Přečte HID flag z NVM footeru (konec NVM). Default False."""
    try:
        nvm = microcontroller.nvm
        foot = bytes(nvm[len(nvm) - ff_config.FOOTER_SIZE:])
        info = ff_config.unpack_footer(foot)
        return bool(info and info["hid_enabled"])
    except Exception:
        return False

def _hid_flag_write(enabled):
    """Zapíše validní footer s HID flagem. Vrací True při úspěchu."""
    try:
        nvm = microcontroller.nvm
        flags = ff_config.HID_FLAG_BIT if enabled else 0
        nvm[len(nvm) - ff_config.FOOTER_SIZE:] = ff_config.pack_footer(flags)
        return True
    except Exception:
        return False

_config_source = "defaults"   # "nvm" | "file" | "defaults" — plní load_presets

def load_presets(path=PRESETS_PATH):
    global _config_source
    # 1) NVM v2 (CRC), 2) NVM v1 (migrace), 3) soubor, 4) defaults
    for raw in (_nvm_load_v2(), _nvm_load()):
        if raw:
            try:
                out = parse_banks(json.loads(raw.decode("utf-8")))
                _config_source = "nvm"
                return out
            except Exception:
                pass
    try:
        with open(path, "r") as f:
            out = parse_banks(json.load(f))
        _config_source = "file"
        return out
    except Exception:
        pass
    _config_source = "defaults"
    return DEFAULT_PRESETS

def save_presets():
    """Uloží do NVM v2 (primární) + soubor (bonus). Vrací (ok, reason)."""
    import os
    data = ff_config.serialize_state(banks, button_macro, macro_global, fader_response)
    ok, reason = _nvm_save_v2(data.encode("utf-8"))
    try:
        try:
            os.remove(PRESETS_PATH)
        except Exception:
            pass
        with open(PRESETS_PATH, "w") as f:
            f.write(data)
        if not ok and reason == "nvm_write":
            ok, reason = True, None   # soubor jako záchrana, když NVM selže (ne too_large)
    except Exception:
        pass
    return (ok, reason)

PRESETS = load_presets()
banks = PRESETS["banks"]
hid_enabled = _hid_flag_read()   # runtime kopie HID flagu (footer je persistent zdroj)
button_macro = PRESETS.get("macro_keys", [])   # globální long-press makro (HID usage IDy)
macro_global = PRESETS.get("macro_global", True)   # False = makro se bere z aktivní banky
fader_response = ff_config.normalize_fader_response(PRESETS.get("fader_response"))

# =========================
#  HARDWARE
# =========================
fader1_adc = analogio.AnalogIn(board.A0)
fader2_adc = analogio.AnalogIn(board.A1)
encoder = rotaryio.IncrementalEncoder(board.GP20, board.GP21, divisor=ENCODER_DIVISOR)
last_encoder_pos = encoder.position

button = digitalio.DigitalInOut(board.GP22)
button.switch_to_input(pull=digitalio.Pull.UP)
last_button_state = button.value
last_button_time = 0
BUTTON_DEBOUNCE_MS = 150

BUTTON_LONGPRESS_MS = 500       # práh dlouhého stisku → makro
BUTTON_DOUBLEPRESS_MS = 300     # okno od short-release, ve kterém druhý short-release = double
button_pressed_at = 0
button_long_fired = False
button_armed = False            # vyžaduj plné uvolnění po bootu (DEV-boot hold guard)
button_macro_for_press = []     # makro spočtené jednou na hraně stisku (audit finding 3 —
                                 # bez cache by se s Global vypnutým a prázdným makrem banky
                                 # active_macro_keys() volalo (a alokovalo list) po každé
                                 # iteraci smyčky po celou dobu držení tlačítka)
button_pending_switch = False   # čeká se na možný druhý short-press (double)
button_pending_switch_at = 0

# =========================
#  MIDI
# =========================
midi = adafruit_midi.MIDI(midi_in=usb_midi.ports[0], midi_out=usb_midi.ports[1], in_buf_size=2048)

# =========================
#  BANK STATE
# =========================
bank_index = 0
def current_fader_ccs():    return banks[bank_index]["fader_cc"]
def current_fader_chs():    return banks[bank_index]["fader_ch"]
def current_encoder_cc():   return banks[bank_index]["encoder"]
def current_encoder_ch():   return banks[bank_index]["encoder_ch"]
def current_uacc_values():  return banks[bank_index]["uacc_values"]

# =========================
#  HELPERS
# =========================
def clamp(v, lo=0, hi=127): return max(lo, min(hi, v))

def read_adc_7bit_avg(adc):
    acc = 0
    for _ in range(SAMPLE_AVG):
        acc += adc.value
    return (acc // SAMPLE_AVG) // 512  # 0..127

def read_fader_7bit_inverted_filtered(adc):
    raw = 127 - read_adc_7bit_avg(adc)
    remapped = (raw - FADER_RAW_MIN) / (FADER_RAW_MAX - FADER_RAW_MIN) * 127
    return clamp(round(remapped))

# =========================
#  SYSEX ENCODE / DECODE
# =========================
def dec7(data):
    """Dekóduje 7-bit zakódovaná data (shodný algoritmus s webovou appkou).

    Defenzivně maskuje high bit (0/1) i low 7 bitů, aby poškozený payload
    nevyrobil hodnotu > 255.
    """
    out = []
    i = 0
    while i + 1 < len(data):
        out.append(((data[i] & 1) << 7) | (data[i + 1] & 0x7F))
        i += 2
    return out

def enc7(data):
    """Zakóduje bajty do 7-bit formu (shodný algoritmus s webovou appkou)."""
    out = []
    for b in data:
        out.append((b >> 7) & 1)
        out.append(b & 0x7F)
    return out

def apply_web_config(web_cfg):
    """Aplikuje cfg z webové appky: pure konverze v ff_config.normalize_web_config,
    tady už jen hardware-side binding (banks, encoder_state, controllers).
    Vrací False při neplatné struktuře.
    """
    global banks, bank_index, encoder_state, button_macro, macro_global, fader_response
    new_banks = ff_config.normalize_web_config(web_cfg)
    if not new_banks:
        return False
    # audit finding 2: chybejici "macro_global" = stary klient appky —
    # zachovej macro_global a per-bank macro_keys zarizeni misto ticheho resetu.
    new_macro_global, new_banks = ff_config.merge_legacy_macro_state(
        web_cfg, new_banks, macro_global, banks)
    new_fader_response = ff_config.merge_legacy_fader_response(web_cfg, fader_response)
    banks = new_banks
    button_macro = ff_config.parse_macro_keys(web_cfg.get("macro_keys"))
    macro_global = new_macro_global
    fader_response = new_fader_response
    bank_index = min(bank_index, len(banks) - 1)
    encoder_state = [{"idx": 0} for _ in banks]
    apply_bank_to_controllers()
    return True

_config_hash = ""   # nastaví boot + každý úspěšný zápis


def _recompute_hash():
    global _config_hash
    _config_hash = ff_config.state_hash(ff_config.serialize_state(banks, button_macro, macro_global, fader_response))


def apply_and_save_json(payload_str):
    """Společná cesta serial CMD_W i SysEx CMD_W. Vrací (ok, reason).

    Pořadí je záměrné (audit F3): velikost nového configu se ověří PŘED
    jakoukoliv mutací globálního stavu — na too_large zůstává RAM i NVM
    beze změny (jinak by appka mohla mlčky auto-loadnout neuložený config,
    protože hash by seděl s in-RAM stavem, ne s NVM)."""
    global _config_source
    try:
        web_cfg = json.loads(payload_str)
    except Exception:
        return (False, "parse")
    try:
        new_banks = ff_config.normalize_web_config(web_cfg)
        if not new_banks:
            return (False, "invalid")
        new_macro = ff_config.parse_macro_keys(web_cfg.get("macro_keys"))
        # audit finding 2: stejny merge jako apply_web_config, na stejnem
        # predchozim (jeste nezmutovanem) stavu — hash sedi s tim, co se pak
        # skutecne aplikuje.
        new_macro_global, new_banks = ff_config.merge_legacy_macro_state(
            web_cfg, new_banks, macro_global, banks)
        new_fader_response = ff_config.merge_legacy_fader_response(web_cfg, fader_response)
        data = ff_config.serialize_state(new_banks, new_macro, new_macro_global, new_fader_response)
    except Exception:
        return (False, "invalid")
    blob_len = len(data.encode("utf-8")) + ff_config.NVM_V2_HEADER
    if blob_len > len(microcontroller.nvm) - ff_config.FOOTER_SIZE:
        return (False, "too_large")
    try:
        applied = apply_web_config(web_cfg)
    except Exception:
        return (False, "invalid")
    if not applied:
        return (False, "invalid")
    ok, reason = save_presets()
    if ok:
        _recompute_hash()
        _config_source = "nvm"
    return (ok, reason)

_recompute_hash()   # boot-time config_hash z banks/button_macro načtených v load_presets()

def _device_serial_hex():
    """Hex sériové číslo z microcontroller.cpu.uid, nebo None (final review 2026-08-17, A-6 —
    dřív duplikováno stejně v send_info_sysex() i CMD_INFO větvi handle_serial_line())."""
    try:
        return "".join("{:02X}".format(b) for b in bytes(microcontroller.cpu.uid))
    except Exception:
        return None

def send_info_sysex():
    """Odešle info o zařízení (odpověď na CMD_INFO)."""
    try:
        serial_str = _device_serial_hex()
        info = ff_config.build_info_dict(
            FIRMWARE_VER, MODEL_ID, serial_str,
            hid_available=(_kbd is not None), hid_enabled=_hid_flag_read(),
            supports_14bit=False, supports_macros=False,
            fader_response=fader_response,
            bank=bank_index,
            faders=[read_fader_7bit_inverted_filtered(fader1_adc),
                    read_fader_7bit_inverted_filtered(fader2_adc)],
            update=_update_info(),
        )
        payload = enc7(list(json.dumps(info).encode("utf-8")))
        sysex = bytes([0xF0, MFR, DEV_ID, CMD_INFO] + payload + [0xF7])
        usb_midi.ports[1].write(sysex)
    except Exception:
        pass

def send_config_chunks():
    """Pošle aktuální config jako sekvenci CMD_CHUNK SysEx zpráv (128 bytů/chunk)."""
    CHUNK_SIZE = 128
    data = json.dumps({"banks": banks}).encode("utf-8")
    total = max(1, (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE)
    for idx in range(total):
        chunk = list(data[idx * CHUNK_SIZE:(idx + 1) * CHUNK_SIZE])
        payload = enc7(chunk)
        sysex = bytes([0xF0, MFR, DEV_ID, CMD_CHUNK, idx, total] + payload + [0xF7])
        try:
            usb_midi.ports[1].write(sysex)
        except Exception:
            pass
        time.sleep(0.01)


def set_hid_enabled(enabled):
    """Aplikační logika bez transportu. Vrací (ok, reason)."""
    global hid_enabled
    if _hid_flag_write(bool(enabled)):
        hid_enabled = bool(enabled)
        return (True, None)
    return (False, "hid")

def handle_sysex(msg):
    """Zpracuje příchozí SysEx zprávu od webové appky.

    Formát: 0xF0 MFR DEV_ID CMD ...enc7(JSON)... 0xF7
    adafruit_midi SystemExclusive: msg.manufacturer_id = [MFR],
                                   msg.data = [DEV_ID, CMD, *payload]

    Read-only (final review 2026-08-17, SEC-003): MFR/DEV_ID jsou dva veřejné,
    uhodnutelné bajty, žádná skutečná autentizace — libovolný lokální MIDI
    program je zná. CMD_W/CMD_HID (zápis configu, toggle HID) proto SysEx
    cestou od teď nejdou vůbec, bez ohledu na to, jak dobře MFR/DEV_ID sedí;
    zavřeno je riziko mutace, ne jen zpřísněna identifikace odesílatele.
    Serial zůstává jediná cesta pro zápis (app ho stejně nikdy nepoužívala
    přes SysEx, viz PR-001).
    """
    if list(msg.manufacturer_id) != [MFR]:
        return
    data = msg.data
    if len(data) < 2 or data[0] != DEV_ID:
        return
    cmd     = data[1]

    if cmd == CMD_R:
        send_config_chunks()

    elif cmd == CMD_INFO:
        send_info_sysex()

    else:
        # CMD_W/CMD_HID (a cokoli neznámého) — žádná odpověď, žádná mutace;
        # ticho místo ACK/ERR taky nedává útočníkovi orákulum na probing.
        return

# =========================
#  FIRMWARE UPDATE (serial-only, spec 2026-09-24)
# =========================
_upd = None              # ff_update.UpdateSession | None (stav jen v RAM)
_reset_countdown = None  # iterace smyčky do resetu po CMD_UE (~1 ms/iterace)
RESET_AFTER_ITERATIONS = 200

def _update_info():
    return {"supported": ff_update.HAS_BINASCII, "slot": RUNNING_SLOT}

def handle_update_cmd(cmd, rid, payload, now_ms):
    """CMD_UB/UC/UE/UA → celý řádek odpovědi (bez \\n)."""
    global _upd, _reset_countdown
    try:
        # Během trialu je druhý slot jediná známá dobrá verze; po CMD_UE už
        # /active ukazuje na nový slot a čeká se na reset → jen CMD_UA.
        if cmd != "CMD_UA" and (_update_trial or _reset_countdown is not None):
            raise ff_update.UpdateError("state")
        if not ff_update.HAS_BINASCII:
            raise ff_update.UpdateError("unsupported")
        if cmd == "CMD_UB":
            _upd = None
            try:
                meta = json.loads(payload or "")
            except ValueError:
                raise ff_update.UpdateError("invalid")
            s = ff_update.UpdateSession(UPDATE_ROOT, RUNNING_SLOT)
            s.begin(meta)
            _upd = s
            return "ACK:%s" % rid
        if cmd == "CMD_UA":
            if _upd is not None:
                _upd.abort()
            _upd = None
            return "ACK:%s" % rid
        if _upd is None:
            raise ff_update.UpdateError("state")
        if cmd == "CMD_UC":
            name, off, b64 = ff_update.parse_chunk(payload)
            return "ACK:%s:%d" % (rid, _upd.chunk(name, off, b64))
        _upd.end()                      # CMD_UE
        _upd = None
        _reset_countdown = RESET_AFTER_ITERATIONS
        return "ACK:%s" % rid
    except ff_update.UpdateError as e:
        return "ERR:%s:%s" % (rid, e.code)
    except Exception:
        if _upd is not None:
            try:
                _upd.abort()
            except Exception:
                pass
        _upd = None
        return "ERR:%s:write" % rid

def maybe_reset():
    """Reset po CMD_UE až po RESET_AFTER_ITERATIONS iteracích smyčky (ACK stihne
    odejít přes USB); počítadlo místo float ms — monotonic po dnech zhrubne."""
    global _reset_countdown
    if _reset_countdown is None:
        return
    _reset_countdown -= 1
    if _reset_countdown <= 0:
        microcontroller.reset()

def handle_serial_line(line):
    """Zpracuje jeden kompletní řádek serial protokolu (CMD_R/CMD_INFO/CMD_W/
    CMD_HID) a pošle odpověď. Framing (čtení bytů, hledání \\n) zůstává
    v hlavní smyčce — tahle funkce dostane už dekódovaný, odstripovaný řádek.
    Vytaženo z hlavní smyčky beze změny chování (audit 2026-07-20, A-4) —
    serial je primární config transport a předtím byl jediný nezapouzdřený.
    """
    cmd, rid, payload = ff_config.parse_serial_line(line)

    def _reply(s):
        try:
            usb_cdc.data.write(s.encode("utf-8") + b"\n")
        except Exception:
            pass

    if cmd == "CMD_R":
        body = ff_config.serialize_state(banks, button_macro, macro_global, fader_response)
        _reply("CFG:%s:%s" % (rid, body) if rid else body)
    elif cmd == "CMD_INFO":
        _uid = _device_serial_hex()
        _info = ff_config.build_info_dict(
            FIRMWARE_VER, MODEL_ID, _uid,
            hid_available=(_kbd is not None), hid_enabled=_hid_flag_read(),
            supports_14bit=False, supports_macros=False,
            config_hash=_config_hash, config_source=_config_source,
            bank=bank_index,
            fader_response=fader_response,
            faders=[read_fader_7bit_inverted_filtered(fader1_adc),
                    read_fader_7bit_inverted_filtered(fader2_adc)],
            update=_update_info(),
        )
        body = json.dumps(_info, separators=(",", ":"))
        _reply("INFO:%s:%s" % (rid, body) if rid else body)
    elif cmd == "CMD_W":
        ok, reason = apply_and_save_json(payload)
        if rid:
            _reply("ACK:%s:%s" % (rid, _config_hash) if ok
                   else "ERR:%s:%s" % (rid, reason))
    elif cmd == "CMD_HID":
        try:
            req = json.loads(payload)
            ok, reason = set_hid_enabled(bool(req.get("enabled", False)))
        except Exception:
            ok, reason = False, "parse"
        if rid:
            _reply("ACK:%s" % rid if ok else "ERR:%s:%s" % (rid, reason))
    elif cmd in ("CMD_UB", "CMD_UC", "CMD_UE", "CMD_UA"):
        if rid:
            _reply(handle_update_cmd(cmd, rid, payload, time.monotonic() * 1000))

# =========================
#  FADER CLASS (filtrace + volitelná interpolace)
# =========================
class Fader:
    def __init__(self, adc, idx):
        self.adc = adc
        self.idx = idx          # pozice v banks[i]["fader_cc"] (0 nebo 1) — CC se čte živě, viz _send
        self.boot_val = read_fader_7bit_inverted_filtered(adc)
        self.prev_out = self.boot_val       # co jsme naposled poslali
        self.target   = self.boot_val       # kam míříme (z ADC)
        self.armed = False
        self.last_sent_ms = 0
        self.last_interp_ms = 0

    def reset_arm(self):
        self.boot_val = read_fader_7bit_inverted_filtered(self.adc)
        self.prev_out = self.boot_val
        self.target   = self.boot_val
        self.armed = False
        self.last_sent_ms = 0
        self.last_interp_ms = 0

    def _send(self, midi, channel, value, now_ms):
        # CC se čte živě z aktivní banky (jako kanál přes current_fader_chs) — žádný
        # cached self.cc, takže změna configu (CMD_W) se projeví okamžitě bez rebind/reboot.
        midi.send(ControlChange(current_fader_ccs()[self.idx], value), channel=channel)
        self.prev_out = value
        self.last_sent_ms = now_ms

    def maybe_send(self, midi, channel, now_ms):
        profile = FADER_RESPONSE_PROFILES[fader_response]
        # 1) přečti nový cíl z ADC (invert + filtrace)
        v = read_fader_7bit_inverted_filtered(self.adc)
        # 2) armování – dokud se nepohne od boot hodnoty, nic neposílej
        if not self.armed:
            if abs(v - self.boot_val) >= ARM_DELTA:
                self.armed = True
            else:
                # drž aktuální cíl/prev_out synchronní, ale neodesílej
                self.target = v
                self.prev_out = v
                return

        # Aktualizuj cíl až za profilem danou toleranci — potlačuje ADC jitter na místě.
        # Krajní hodnoty (0, 127) přijmi vždy — jinak by tam tolerance nikdy nedovolila dojet.
        if v in (0, 127) or abs(v - self.target) >= profile["tolerance"]:
            self.target = v

        if not profile["interpolation"]:
            # === Bez interpolace: klasická tolerance + hysterese + rate-limit ===
            if (now_ms - self.last_sent_ms) >= profile["rate_limit_ms"]:
                if abs(self.target - self.prev_out) >= (profile["tolerance"] + (profile["hysteresis"] if self.target != self.prev_out else 0)):
                    self._send(midi, channel, self.target, now_ms)
            return

        # === S interpolací ===
        # Posunuj prev_out směrem k target v profilem daných pravidelných krocích,
        # ale ne častěji, než dovolí jeho rate limit.
        if (now_ms - self.last_interp_ms) < profile["interp_step_ms"]:
            return
        self.last_interp_ms = now_ms

        diff = self.target - self.prev_out
        if diff == 0:
            return

        # velikost kroku (alespoň 1, jinak by se to „zaseklo”)
        step = int(abs(diff) * profile["interp_speed"])
        if step < profile["interp_min_step"]:
            step = profile["interp_min_step"]
        if diff < 0:
            step = -step

        # dodrž rate-limit
        if (now_ms - self.last_sent_ms) < profile["rate_limit_ms"]:
            return

        next_val = clamp(self.prev_out + step)
        # pokud krok přeskočí cíl, rovnou nastav cíl
        if (diff > 0 and next_val > self.target) or (diff < 0 and next_val < self.target):
            next_val = self.target

        # pošli další mezihodnotu
        self._send(midi, channel, next_val, now_ms)

    def snapshot(self, midi, channel):
        self._send(midi, channel, self.prev_out, time.monotonic() * 1000)

# =========================
#  ENCODER STATE (diskrétní UACC hodnoty)
# =========================
encoder_state = [{'idx': 0} for _ in banks]      # výchozí = nejnižší UACC
encoder_boot_quiet_done = SEND_INITIAL_ENCODER_SNAPSHOT_ON_BOOT

def encoder_current_value():
    idx = encoder_state[bank_index]['idx']
    return current_uacc_values()[idx]

def encoder_snapshot():
    cc_num = current_encoder_cc()
    val = encoder_current_value()
    midi.send(ControlChange(cc_num, val), channel=current_encoder_ch())

def apply_bank_to_controllers():
    # CC/kanál se čtou živě při odesílání (viz Fader._send / current_fader_chs),
    # takže tady stačí re-armovat fadery, aby po změně banky/configu neposlaly skok.
    fader1_obj.reset_arm()
    fader2_obj.reset_arm()

def on_bank_changed():
    # Ohlaš novou banku appce (Program Change na ch 0) — app handler 0xC0 přepne UI
    try:
        midi.send(ProgramChange(bank_index), channel=0)
    except Exception:
        pass
    apply_bank_to_controllers()
    if SEND_FADER_SNAPSHOT_ON_BANK_CHANGE:
        f_ch = current_fader_chs()
        fader1_obj.snapshot(midi, f_ch[0])
        fader2_obj.snapshot(midi, f_ch[1])
    if SEND_ENCODER_SNAPSHOT_ON_BANK_CHANGE and banks[bank_index].get("roller_mode", "cc") == "cc":
        encoder_snapshot()
    # Pozice enkodéru se per banku NEresetuje — zůstává zapamatovaná (snapshot i interní stav sedí)

def handle_encoder_delta(delta, now_ms):
    """Zpracuje nenulový pohyb enkodéru podle aktuálního roller_mode banky
    (cc/keyswitch/track_nav). Vytaženo z hlavní smyčky beze změny chování
    (audit 2026-07-20, A-4); volající si sám hlídá last_encoder_pos."""
    global encoder_boot_quiet_done
    mode = banks[bank_index].get("roller_mode", "cc")
    if mode == "keyswitch":
        ks = banks[bank_index]["ks_notes"]
        if ks:
            st = encoder_state[bank_index]
            # The mechanical roller reports a negative delta when moved up.
            # Keyswitch lists are ordered low→high, so invert only this
            # sequence index: rolling up must advance to the next value.
            new_idx = clamp(st['idx'] - delta, 0, len(ks) - 1)
            if new_idx != st['idx']:
                st['idx'] = new_idx
                note = ks[new_idx]
                if encoder_boot_quiet_done:
                    ch = banks[bank_index]["ks_channel"]
                    vel = banks[bank_index]["ks_velocity"]
                    midi.send(NoteOn(note, vel), channel=ch)
                    pending_noteoffs.append((note, ch, now_ms + KEYSWITCH_NOTEOFF_MS))
                else:
                    encoder_boot_quiet_done = True
    elif mode == "track_nav":
        if hid_enabled and _kbd is not None and encoder_boot_quiet_done:
            b = banks[bank_index]
            cw = (delta > 0) != bool(b["nav_invert"])   # True = "po směru" akce
            keys = b["nav_keys_cw"] if cw else b["nav_keys_ccw"]
            n = min(abs(delta), NAV_MAX_BURST)
            for _ in range(n):
                try:
                    _kbd.send(*keys)
                except Exception:
                    break
        elif not encoder_boot_quiet_done:
            encoder_boot_quiet_done = True   # první pohyb po bootu potlačit
    elif mode == "cc_relative":
        if encoder_boot_quiet_done:
            cc_num = current_encoder_cc()
            ch = current_encoder_ch()
            val = 1 if delta < 0 else 127
            n = min(abs(delta), NAV_MAX_BURST)
            for _ in range(n):
                midi.send(ControlChange(cc_num, val), channel=ch)
        else:
            encoder_boot_quiet_done = True
    else:
        # mode == "cc" (default)
        st = encoder_state[bank_index]
        uacc = current_uacc_values()
        new_idx = clamp(st['idx'] + delta, 0, len(uacc) - 1)
        if new_idx != st['idx']:
            st['idx'] = new_idx
            val = uacc[new_idx]
            if encoder_boot_quiet_done:
                midi.send(ControlChange(current_encoder_cc(), val), channel=current_encoder_ch())
            else:
                encoder_boot_quiet_done = True

def handle_button(now_ms):
    """Zpracuje tlačítko (short = bank switch na release, long >=500ms =
    makro). Vytaženo z hlavní smyčky beze změny chování (stejný vzor jako
    handle_encoder_delta, audit 2026-07-20 A-4) — navíc audit finding 3:
    makro pro dlouhý stisk se počítá jednou na hraně stisku
    (button_macro_for_press), ne znovu v každé iteraci po celou dobu
    držení.

    Short-release nepřepne banku hned — čeká BUTTON_DOUBLEPRESS_MS, jestli
    nepřijde druhý short-release (double-press → předchozí banka místo další).
    Timeout commit (jednoduchý short-press → další banka) běží mimo hranu,
    protože handle_button() se volá i bez změny stavu tlačítka (hlavní smyčka)."""
    global last_button_state, last_button_time, button_armed
    global button_pressed_at, button_long_fired, button_macro_for_press, bank_index
    global button_pending_switch, button_pending_switch_at
    state = button.value  # True = released, False = pressed
    if not button_armed:
        if state is True:                       # počkej na první uvolnění (DEV-boot guard)
            button_armed = True
    elif (last_button_state is True) and (state is False):
        # hrana stisku (debounce)
        if (now_ms - last_button_time) > BUTTON_DEBOUNCE_MS:
            button_pressed_at = now_ms
            button_long_fired = False
            button_macro_for_press = ff_config.active_macro_keys(macro_global, button_macro, banks[bank_index])
            last_button_time = now_ms
    elif (state is False) and (not button_long_fired) and ((now_ms - button_pressed_at) >= BUTTON_LONGPRESS_MS):
        # drženo přes práh → odpal makro jednou; bez makra dlouhý stisk
        # degraduje na short-press (bank switch při release) — audit F1
        _macro = button_macro_for_press
        if hid_enabled and _kbd is not None and _macro:
            button_long_fired = True
            try:
                _kbd.send(*_macro)
            except Exception:
                try:
                    _kbd.release_all()
                except Exception:
                    pass
    elif (last_button_state is False) and (state is True):
        # hrana uvolnění po short-pressu (jen když neproběhlo makro)
        if not button_long_fired:
            if button_pending_switch and (now_ms - button_pending_switch_at) <= BUTTON_DOUBLEPRESS_MS:
                # druhý short-release v okně = double-press → předchozí banka
                button_pending_switch = False
                bank_index = (bank_index - 1) % len(banks)
                on_bank_changed()
            else:
                # první short-release → počkej, jestli nepřijde double
                button_pending_switch = True
                button_pending_switch_at = now_ms
    if button_pending_switch and (now_ms - button_pending_switch_at) > BUTTON_DOUBLEPRESS_MS:
        # okno vypršelo bez druhého stisku → potvrzen single-press → další banka
        button_pending_switch = False
        bank_index = (bank_index + 1) % len(banks)
        on_bank_changed()
    last_button_state = state

# =========================
#  INIT CONTROLLERS
# =========================
fader1_obj = Fader(fader1_adc, 0)
fader2_obj = Fader(fader2_adc, 1)

if SEND_INITIAL_FADER_SNAPSHOT_ON_BOOT:
    _f_ch = current_fader_chs()
    fader1_obj.snapshot(midi, _f_ch[0])
    fader2_obj.snapshot(midi, _f_ch[1])
if SEND_INITIAL_ENCODER_SNAPSHOT_ON_BOOT:
    encoder_snapshot()

# =========================
#  MAIN LOOP
# =========================
_serial_buf = bytearray()
MAX_SERIAL_BUF = 8192   # rámec bez ukončovacího \n nad tento limit zahodit (ochrana RAM)
pending_noteoffs = []   # list of (note, channel, due_ms)
_boot_ms = time.monotonic() * 1000
_update_trial = ff_update.is_trial(UPDATE_ROOT)

def maybe_confirm_update(now_ms):
    """Po CONFIRM_AFTER_MS běhu smyčky potvrdí novou verzi (smaže /pending)."""
    global _update_trial
    if _update_trial and now_ms - _boot_ms >= ff_update.CONFIRM_AFTER_MS:
        try:
            ff_update.confirm(UPDATE_ROOT)
        except OSError:
            pass
        _update_trial = False

while True:
    now_ms = time.monotonic() * 1000

    # --- Pending note-offs (keyswitch) ---
    if pending_noteoffs:
        due, pending_noteoffs = ff_config.due_noteoffs(pending_noteoffs, now_ms)
        for note, ch, _ in due:
            midi.send(NoteOff(note, 0), channel=ch)

    # --- MIDI IN (SysEx od webové appky) ---
    msg = midi.receive()
    if msg is not None and isinstance(msg, SystemExclusive):
        handle_sysex(msg)

    # --- SERIAL (CMD_R / CMD_W od webové appky) ---
    if usb_cdc.data and usb_cdc.data.in_waiting:
        try:
            chunk = usb_cdc.data.read(usb_cdc.data.in_waiting)
            _serial_buf.extend(chunk)
            if len(_serial_buf) > MAX_SERIAL_BUF and b"\n" not in _serial_buf:
                del _serial_buf[:]   # přerostlý rámec bez ukončení – zahodit
            if b"\n" in _serial_buf:
                nl = _serial_buf.index(b"\n")
                line = _serial_buf[:nl].decode("utf-8").strip()
                _serial_buf = _serial_buf[nl + 1:]
                handle_serial_line(line)
        except Exception:
            pass

    # --- FADERS ---
    f_ch = current_fader_chs()
    fader1_obj.maybe_send(midi, f_ch[0], now_ms)
    fader2_obj.maybe_send(midi, f_ch[1], now_ms)

    # --- ENCODER (větveno podle roller_mode) ---
    pos = encoder.position
    delta = pos - last_encoder_pos
    if delta != 0:
        handle_encoder_delta(delta, now_ms)
        last_encoder_pos = pos

    # --- BUTTON (short = bank switch na release, long >=500ms = makro) ---
    handle_button(now_ms)

    maybe_confirm_update(now_ms)
    maybe_reset()
    time.sleep(0.001)
