# Aplikátor záplat cudzích doplnkov. Bez importov Kodi, aby sa dal testovať offline.
#
# Záplata je unified diff (z originálu na našu verziu) uložený v patches/.
# Aplikátor je prísny: bez fuzz a bez posunu riadkov, kontext aj odstránené
# riadky musia sedieť presne. Zapíše sa len výsledok, ktorý má presne
# patched_sha256 z manifestu, takže doplnok s inou verziou kódu zostane nedotknutý.
import hashlib
import json
import os
import re
import stat
import tempfile

PATCHES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'patches')
BACKUP_SUFFIX = '.orig-bigtime'

NEPRITOMNY = 'NEPRITOMNY'   # doplnok alebo súbor nie je nainštalovaný
OK = 'OK'                   # súbor je už záplatovaný
CHYBA = 'CHYBA'             # originál, záplatu možno nasadiť
INE = 'INE'                 # iný obsah (nová verzia doplnku), nič sa nemení
NASADENE = 'NASADENE'       # výsledok apply(): záplata sa práve zapísala

_ADDON_ID = re.compile(r'^[A-Za-z0-9_.\-]+$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
_KEYS = ('addon_id', 'version', 'rel_path', 'orig_sha256', 'patched_sha256', 'patch')


class PatchError(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def split_lines(text):
    """Rozdelí text na riadky aj s koncami, len podľa \\n (\\r ostáva v riadku)."""
    parts = text.split('\n')
    lines = [p + '\n' for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def apply_unified(orig_bytes, diff_text):
    """Prísne aplikuje unified diff na bajty. Pri akejkoľvek nezhode PatchError."""
    try:
        orig_lines = split_lines(orig_bytes.decode('utf-8'))
    except UnicodeDecodeError as exc:
        raise PatchError('originál nie je UTF-8: %s' % exc)

    diff = diff_text.split('\n')
    if diff and diff[-1] == '':
        diff.pop()

    out = []
    cursor = 0          # počet riadkov originálu, ktoré sú už spracované
    i = 0
    seen_hunk = False
    while i < len(diff):
        line = diff[i]
        m = _HUNK.match(line)
        if not m:
            if seen_hunk:
                raise PatchError('neočakávaný riadok mimo hunku: %r' % line[:60])
            i += 1      # hlavičky --- / +++ pred prvým hunkom
            continue
        seen_hunk = True
        old_start = int(m.group(1))
        old_len = int(m.group(2)) if m.group(2) is not None else 1
        new_len = int(m.group(4)) if m.group(4) is not None else 1
        i += 1

        old_chunk, new_chunk = [], []
        last = None     # kam patrí prípadný „\ No newline at end of file“
        while len(old_chunk) < old_len or len(new_chunk) < new_len or \
                (i < len(diff) and diff[i].startswith('\\')):
            if i >= len(diff):
                raise PatchError('hunk je skrátený')
            d = diff[i]
            i += 1
            tag, body = (d[:1], d[1:]) if d else (' ', '')
            if tag == '\\':
                if last is None:
                    raise PatchError('nečakaná značka konca súboru')
                for chunk in last:
                    if chunk[-1].endswith('\n'):
                        chunk[-1] = chunk[-1][:-1]
                last = None
                continue
            text = body + '\n'
            if tag == ' ':
                old_chunk.append(text)
                new_chunk.append(text)
                last = (old_chunk, new_chunk)
            elif tag == '-':
                old_chunk.append(text)
                last = (old_chunk,)
            elif tag == '+':
                new_chunk.append(text)
                last = (new_chunk,)
            else:
                raise PatchError('neznámy riadok v hunku: %r' % d[:60])
            if len(old_chunk) > old_len or len(new_chunk) > new_len:
                raise PatchError('hunk má viac riadkov, než hlási hlavička')

        # pri prázdnom starom rozsahu je old_start číslo riadku PRED vložením
        idx = old_start if old_len == 0 else old_start - 1
        if idx < cursor:
            raise PatchError('hunky sa prekrývajú alebo nie sú zoradené')
        if idx + old_len > len(orig_lines):
            raise PatchError('hunk presahuje koniec súboru')
        if orig_lines[idx:idx + old_len] != old_chunk:
            raise PatchError('kontext hunku (riadok %d) nesedí s originálom' % old_start)
        out.extend(orig_lines[cursor:idx])
        out.extend(new_chunk)
        cursor = idx + old_len

    if not seen_hunk:
        raise PatchError('záplata neobsahuje žiadny hunk')
    out.extend(orig_lines[cursor:])
    return ''.join(out).encode('utf-8')


def make_unified(orig_bytes, new_bytes, label):
    """Vyrobí unified diff (n=3). Používa ho len tools/make_patches.py."""
    import difflib
    a = split_lines(orig_bytes.decode('utf-8'))
    b = split_lines(new_bytes.decode('utf-8'))
    out = []
    for line in difflib.unified_diff(a, b, 'a/' + label, 'b/' + label, n=3, lineterm='\n'):
        out.append(line)
        if not line.endswith('\n'):
            out.append('\n\\ No newline at end of file\n')
    return ''.join(out)


# --- manifest ---------------------------------------------------------------

def _rel_parts(rel_path):
    if not isinstance(rel_path, str) or not rel_path or '\\' in rel_path or '\0' in rel_path:
        raise PatchError('neplatná rel_path')
    if rel_path.startswith('/') or re.match(r'^[A-Za-z]:', rel_path):
        raise PatchError('rel_path nesmie byť absolútna')
    parts = rel_path.split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise PatchError('rel_path obsahuje neplatnú zložku')
    return parts


def validate_entry(entry):
    for k in _KEYS:
        if not isinstance(entry.get(k), str) or not entry[k]:
            raise PatchError('v manifeste chýba %s' % k)
    if not _ADDON_ID.match(entry['addon_id']) or entry['addon_id'] in ('.', '..'):
        raise PatchError('neplatné addon_id')
    if not _SHA.match(entry['orig_sha256']) or not _SHA.match(entry['patched_sha256']):
        raise PatchError('neplatný sha256 v manifeste')
    _rel_parts(entry['rel_path'])
    _rel_parts(entry['patch'])


def load_manifest(patches_dir=None):
    path = os.path.join(patches_dir or PATCHES_DIR, 'manifest.json')
    with open(path, 'rb') as f:
        data = json.loads(f.read().decode('utf-8'))
    entries = data.get('patches') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise PatchError('manifest má zlý formát')
    return entries


# --- cieľový súbor ----------------------------------------------------------

def target_path(addons_dir, entry):
    """Vráti bezpečnú cestu k cieľovému súboru, alebo None, keď doplnok chýba.

    Odmietne `..`, absolútne cesty a symlinky v ceste pod adresárom doplnku
    (vrátane samotného súboru). Samotný adresár doplnku môže byť symlink.
    """
    validate_entry(entry)
    parts = _rel_parts(entry['rel_path'])
    addon_dir = os.path.join(addons_dir, entry['addon_id'])
    if not os.path.isdir(addon_dir):
        return None
    base = os.path.realpath(addon_dir)
    cur = addon_dir
    for n, part in enumerate(parts):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return None
        except NotADirectoryError:
            return None
        if stat.S_ISLNK(st.st_mode):
            raise PatchError('symlink v ceste k cieľu: %s' % part)
        last = n == len(parts) - 1
        if last and not stat.S_ISREG(st.st_mode):
            raise PatchError('cieľ nie je obyčajný súbor')
        if not last and not stat.S_ISDIR(st.st_mode):
            return None
    real = os.path.realpath(cur)
    if os.path.commonpath([base, real]) != base:
        raise PatchError('cieľ je mimo adresára doplnku')
    return cur


def inspect(addons_dir, entry):
    """(stav, sha256 súboru alebo None)."""
    path = target_path(addons_dir, entry)
    if path is None:
        return NEPRITOMNY, None
    with open(path, 'rb') as f:
        data = f.read()
    digest = sha256(data)
    if digest == entry['patched_sha256']:
        return OK, digest
    if digest == entry['orig_sha256']:
        return CHYBA, digest
    return INE, digest


def check(addons_dir, entry):
    return inspect(addons_dir, entry)[0]


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.bigtime-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _drop_pyc(path):
    name = os.path.basename(path)
    if not name.endswith('.py'):
        return
    mod = name[:-3] + '.'
    cache = os.path.join(os.path.dirname(path), '__pycache__')
    try:
        names = os.listdir(cache)
    except OSError:
        return
    for n in names:
        if n.startswith(mod) and n.endswith('.pyc'):
            try:
                os.unlink(os.path.join(cache, n))
            except OSError:
                pass


def read_patch(entry, patches_dir=None):
    parts = _rel_parts(entry['patch'])
    base = patches_dir or PATCHES_DIR
    with open(os.path.join(base, *parts), 'rb') as f:
        return f.read().decode('utf-8')


def apply(addons_dir, entry, patches_dir=None):
    """Nasadí záplatu, ak je súbor presne originál. Vráti NASADENE alebo stav
    z check() (OK / INE / NEPRITOMNY), keď sa nemalo nič robiť.
    PatchError, ak sa záplata nedá vyrobiť alebo výsledok nesedí; vtedy sa nezapíše nič.
    """
    path = target_path(addons_dir, entry)
    if path is None:
        return NEPRITOMNY
    with open(path, 'rb') as f:
        orig = f.read()
    digest = sha256(orig)
    if digest == entry['patched_sha256']:
        return OK
    if digest != entry['orig_sha256']:
        return INE
    new = apply_unified(orig, read_patch(entry, patches_dir))
    if sha256(new) != entry['patched_sha256']:
        raise PatchError('výsledok záplaty nemá očakávaný sha256')
    backup = path + BACKUP_SUFFIX
    if not os.path.lexists(backup):
        _atomic_write_new(backup, orig)
    _atomic_write(path, new)
    _drop_pyc(path)
    return NASADENE


def _atomic_write_new(path, data):
    """Vytvorí nový súbor (zálohu) atómovo; existujúci nikdy neprepíše."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.bigtime-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.lexists(path):
            raise PatchError('záloha už existuje')
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
