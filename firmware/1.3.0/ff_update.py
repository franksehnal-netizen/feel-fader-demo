# ff_update.py — aktualizace firmwaru z web appky (slot bookkeeping + upload).
# Formát /active a /pending je ZRCADLO kořenového code.py (zavaděč nesmí
# importovat ze slotu) — test_ff_update_slots.py hlídá shodu.
# Spec: docs/superpowers/specs/2026-09-24-in-app-firmware-update-design.md
import os

try:
    import binascii
    HAS_BINASCII = True
except ImportError:
    HAS_BINASCII = False

CONFIRM_AFTER_MS = 10000


def _read(path):
    # Zrcadlo code.py._read: omezené čtení, poškozený soubor = chybí.
    try:
        with open(path) as f:
            return f.read(64).strip()
    except Exception:
        return None


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def other(slot):
    return "b" if slot == "a" else "a"


def active_slot(root):
    s = _read(root + "active")
    return s if s in ("a", "b") else "a"


def read_version(root):
    v = _read(root + "app_" + active_slot(root) + "/VERSION")
    return v or "0.0.0"


def is_trial(root):
    p = _read(root + "pending")
    return bool(p) and p.split(":")[0] == active_slot(root)


def confirm(root):
    if is_trial(root):
        _remove(root + "pending")


# ---------------------------------------------------------------------
#  Upload session (CMD_UB / CMD_UC / CMD_UE / CMD_UA)
# ---------------------------------------------------------------------
MAX_FILES = 8
MAX_FILE_SIZE = 200000
MAX_NAME = 32
SPACE_MARGIN = 4096
_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"
_VERSION_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz.-"
_HEX = "0123456789abcdef"
# CircuitPython binascii nemusí mít .Error — base64 chyby pak hází ValueError.
_B64_ERR = getattr(binascii, "Error", ValueError) if HAS_BINASCII else ValueError


class UpdateError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def valid_name(name):
    if not isinstance(name, str) or not name.endswith(".py"):
        return False
    stem = name[:-3]
    return 0 < len(stem) and len(name) <= MAX_NAME and all(c in _NAME_CHARS for c in stem)


def parse_chunk(payload):
    try:
        name, off, b64 = (payload or "").split(":", 2)
        off = int(off)
    except ValueError:
        raise UpdateError("invalid")
    if off < 0:
        raise UpdateError("invalid")
    return name, off, b64


def _errno(e):
    return getattr(e, "errno", None) or (e.args[0] if e.args else None)


def _clear_dir(path):
    try:
        names = os.listdir(path)
    except OSError:
        os.mkdir(path)
        return
    for n in names:
        os.remove(path + "/" + n)


def _size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return 0


def _crc_file(path):
    crc = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(512)
            if not b:
                break
            crc = binascii.crc32(b, crc)
    return "%08x" % (crc & 0xFFFFFFFF)


def _statvfs_free():
    st = os.statvfs("/")
    return st[0] * st[4]


def _valid_meta(meta):
    if not isinstance(meta, dict):
        return None
    version = meta.get("version")
    files = meta.get("files")
    if not isinstance(version, str) or not (0 < len(version) <= 16):
        return None
    if not all(c in _VERSION_CHARS for c in version):
        return None
    if not isinstance(files, list) or not (0 < len(files) <= MAX_FILES):
        return None
    out = {}
    for f in files:
        if not isinstance(f, dict):
            return None
        name, size, crc = f.get("name"), f.get("size"), f.get("crc")
        if not valid_name(name) or name in out:
            return None
        if not isinstance(size, int) or not (0 < size <= MAX_FILE_SIZE):
            return None
        if not isinstance(crc, str) or len(crc) != 8 or not all(c in _HEX for c in crc):
            return None
        out[name] = (size, crc)
    return version, out


class UpdateSession:
    def __init__(self, root, running_slot, free_bytes=None):
        # Cíl = slot, ze kterého NEběžíme (ne /active na disku — ten se po
        # CMD_UE mění ještě před restartem).
        self.root = root
        self.target = other(running_slot)
        self.dir = root + "app_" + self.target
        self._free = free_bytes or _statvfs_free
        self.files = None
        self.version = None

    def begin(self, meta):
        parsed = _valid_meta(meta)
        if parsed is None:
            raise UpdateError("invalid")
        version, files = parsed
        need = sum(s for s, _ in files.values()) + SPACE_MARGIN
        try:
            if self._free() < need:
                raise UpdateError("space")
            _clear_dir(self.dir)
        except OSError as e:
            raise UpdateError("dev_mode" if _errno(e) == 30 else "write")
        self.version, self.files = version, files

    def chunk(self, name, offset, b64):
        if self.files is None:
            raise UpdateError("state")
        if name not in self.files:
            raise UpdateError("invalid")
        path = self.dir + "/" + name
        cur = _size(path)
        if offset != cur:
            raise UpdateError("offset:%d" % cur)
        try:
            data = binascii.a2b_base64(b64)
        except (ValueError, _B64_ERR):
            raise UpdateError("invalid")
        if not data or cur + len(data) > self.files[name][0]:
            raise UpdateError("invalid")
        try:
            with open(path, "ab") as f:
                f.write(data)
        except OSError:
            raise UpdateError("write")
        return cur + len(data)

    def end(self):
        if self.files is None:
            raise UpdateError("state")
        for name, (size, crc) in self.files.items():
            path = self.dir + "/" + name
            if _size(path) != size or _crc_file(path) != crc:
                raise UpdateError("crc:" + name)
        try:
            _write(self.dir + "/VERSION", self.version)
            _write(self.root + "pending", "%s:0" % self.target)
            _write(self.root + "active", self.target)
        except OSError:
            raise UpdateError("write")
        self.files = None
        return self.target

    def abort(self):
        self.files = None
        try:
            _clear_dir(self.dir)
        except OSError:
            pass
