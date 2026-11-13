# Spojí playlisty a EPG z O2 TV, Magio GO a Streamletu do jedného playlistu a
# jedného XMLTV, aby ich čítala JEDNA inštancia IPTV Simple. Kodi totiž skupiny
# kanálov z rôznych inštancií nespája (Šport by bol trikrát). Bez importov Kodi,
# žiadna sieť, len súbory, ktoré už exportujú samotné doplnky.
#
# Pravidlá:
#  - zdroje vo vlastných súboroch sa nikdy nemenia, píšu sa len výstupy (atómovo),
#  - kanály sa preberajú po riadkoch tak, ako ich doplnok vyrobil (skupiny,
#    catchup atribúty aj #EXTVLCOPT ostávajú), mení sa len tvg-chno pri kolízii,
#  - znova sa spája len keď sa niektorý zdroj zmenil (veľkosť + čas),
#  - EPG Streamletu (~17 MB, stovky kanálov) sa orezáva na kanály z playlistu;
#    výsledok sa drží v cache, kým sa nezmení EPG alebo zoznam kanálov.
import collections
import hashlib
import html
import json
import os
import re
import tempfile

MARK = '#BIGTIME-MERGED'
FIRST_FREE = 1000          # od tohto čísla sa prideľujú čísla kanálom v kolízii
_ATTR = re.compile(r'([\w:-]+)="([^"]*)"')

Source = collections.namedtuple('Source', 'id label playlist epg filter_epg')


# --- playlist ---------------------------------------------------------------

def _comma_index(line):
    """Index prvej čiarky mimo úvodzoviek (oddeľuje atribúty od názvu), alebo -1."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ',' and not quoted:
            return i
    return -1


def parse_playlist(text):
    """Zoznam kanálov: dict(extinf, extras, url, attrs, name). Hlavička #EXTM3U
    a riadky bez adresy sa zahodia."""
    entries = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTINF'):
            cur = {'extinf': line, 'extras': []}
        elif line.startswith('#'):
            if cur is not None:
                cur['extras'].append(line)
        elif cur is not None:
            cur['url'] = line
            i = _comma_index(cur['extinf'])
            head = cur['extinf'] if i < 0 else cur['extinf'][:i]
            cur['attrs'] = dict(_ATTR.findall(head))
            cur['name'] = '' if i < 0 else cur['extinf'][i + 1:].strip()
            entries.append(cur)
            cur = None
    return entries


def set_chno(extinf, number):
    """Nastaví tvg-chno (existujúci atribút nahradí, inak ho vloží pred názov)."""
    value = 'tvg-chno="%d"' % number
    if re.search(r'tvg-chno="[^"]*"', extinf):
        return re.sub(r'tvg-chno="[^"]*"', value, extinf, count=1)
    i = _comma_index(extinf)
    return extinf + ' ' + value if i < 0 else extinf[:i] + ' ' + value + extinf[i:]


def _chno(entry):
    try:
        n = int(entry['attrs'].get('tvg-chno', ''))
        return n if n > 0 else None
    except ValueError:
        return None


def combine(groups):
    """groups = [(Source, entries)] v poradí priority. Vráti (riadky, počty, vynechané).
    Číslo kanála sa zachová, ak je voľné; kolízia dostane prvé voľné od FIRST_FREE.
    Kanál s tvg-id z PREDCHÁDZAJÚCEHO zdroja sa vynechá (duplicity v rámci jedného
    zdroja ostávajú, tie IPTV Simple zvláda)."""
    used, seen_ids = set(), set()
    counts, skipped, lines = {}, [], []
    nxt = FIRST_FREE
    for src, entries in groups:
        counts[src.id] = 0
        own_ids = set()
        for e in entries:
            tid = e['attrs'].get('tvg-id', '')
            if tid:
                if tid in seen_ids:
                    skipped.append((src.id, tid))
                    continue
                own_ids.add(tid)
            n = _chno(e)
            if n is None or n in used:
                while nxt in used:
                    nxt += 1
                n = nxt
            used.add(n)
            lines.append(set_chno(e['extinf'], n))
            lines.extend(e['extras'])
            lines.append(e['url'])
            counts[src.id] += 1
        seen_ids |= own_ids
    return lines, counts, skipped


# --- EPG --------------------------------------------------------------------

_CHANNEL = re.compile(rb'<channel\b[^>]*>.*?</channel>', re.S)
_PROGRAMME = re.compile(rb'<programme\b[^>]*>.*?</programme>', re.S)
_ID = re.compile(rb'''\bid\s*=\s*(?:"([^"]*)"|'([^']*)')''')
_PCHAN = re.compile(rb'''\bchannel\s*=\s*(?:"([^"]*)"|'([^']*)')''')
_DISPLAY = re.compile(rb'<display-name[^>]*>(.*?)</display-name>', re.S)
_DECL = re.compile(rb'^\s*<\?xml[^>]*encoding\s*=\s*["\']([^"\']+)["\']', re.I)


def _utf8(data):
    """Bajty XMLTV v UTF-8 (iné kódovanie z deklarácie sa prekóduje)."""
    m = _DECL.match(data[:300])
    enc = m.group(1).decode('ascii', 'replace').lower() if m else 'utf-8'
    if enc in ('utf-8', 'utf8'):
        return data
    return data.decode(enc, 'replace').encode('utf-8')


def epg_body(data):
    """Obsah medzi <tv ...> a </tv> (bajty UTF-8), alebo None, ak súbor nie je celý."""
    data = _utf8(data)
    start = data.find(b'<tv')
    if start < 0:
        return None
    gt = data.find(b'>', start)
    end = data.rfind(b'</tv>')
    if gt < 0 or end < gt:
        return None
    return data[gt + 1:end].strip(b'\r\n') + b'\n'


def _first(m):
    return (m.group(1) if m.group(1) is not None else m.group(2)) if m else None


def filter_epg(body, ids, names):
    """Nechá len kanály, ktorých id (bez ohľadu na veľkosť písmen) je v `ids`
    alebo ktorých display-name je v `names` (IPTV Simple páruje aj podľa názvu),
    a ich programy. `ids`/`names` sú množiny malých písmen."""
    kept, out = set(), []
    for m in _CHANNEL.finditer(body):
        block = m.group(0)
        cid = _first(_ID.search(block[:block.index(b'>')]))
        if cid is None:
            continue
        cid = html.unescape(cid.decode('utf-8', 'replace'))
        dn = [html.unescape(d.decode('utf-8', 'replace')).strip().lower()
              for d in _DISPLAY.findall(block)]
        if cid.lower() in ids or any(d in names for d in dn):
            kept.add(cid)
            out.append(block)
    for m in _PROGRAMME.finditer(body):
        block = m.group(0)
        cid = _first(_PCHAN.search(block[:block.index(b'>')]))
        if cid is not None and html.unescape(cid.decode('utf-8', 'replace')) in kept:
            out.append(block)
    return b'\n'.join(out) + b'\n'


# --- súbory -----------------------------------------------------------------

def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except OSError:
        return None


def _write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.bigtime-', suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_state(path):
    try:
        with open(path, 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sha(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(repr(p).encode('utf-8'))
    return h.hexdigest()


def merge(sources, out_m3u, out_epg, state_path, cache_dir, log=None):
    """Vyrobí spoločný playlist a EPG. Vráti True, ak sa výstupy prepísali.
    Zdroj bez playlistu alebo bez kanálov sa preskočí; bez žiadneho zdroja sa
    nezapíše nič."""
    say = log or (lambda msg: None)
    state = _load_state(state_path)
    sig = _sha([(s.id, _stat(s.playlist), _stat(s.epg)) for s in sources])
    if (state.get('sig') == sig and os.path.isfile(out_m3u) and os.path.isfile(out_epg)):
        return False

    groups = []
    for s in sources:
        raw = _read(s.playlist)
        entries = parse_playlist(raw.decode('utf-8', 'replace')) if raw else []
        if entries:
            groups.append((s, entries))
    if not groups:
        return False

    lines, counts, skipped = combine(groups)
    for sid, tid in skipped:
        say('duplicitné tvg-id %s (%s) vynechané' % (tid, sid))

    bodies, filtered = [], dict(state.get('filtered', {}))
    for s, entries in groups:
        raw = _read(s.epg) if s.epg else None
        if raw is None:
            continue
        if not s.filter_epg:
            body = epg_body(raw)
        else:
            ids = {e['attrs'].get('tvg-id', '').lower() for e in entries} - {''}
            names = {n.strip().lower() for e in entries
                     for n in (e['name'], e['attrs'].get('tvg-name', '')) if n.strip()}
            key = _sha(_stat(s.epg), sorted(ids), sorted(names))
            cache = os.path.join(cache_dir, 'epg-%s.xml' % s.id)
            cached = filtered.get(s.id)
            body = _read(cache) if cached == key else None
            if body is None:
                full = epg_body(raw)
                body = filter_epg(full, ids, names) if full is not None else None
                if body is not None:
                    _write(cache, body)
                    filtered[s.id] = key
                    say('EPG %s zúžené na kanály z playlistu (%d kB)' % (s.id, len(body) // 1024))
        if body is None:
            say('EPG %s je neúplné alebo nečitateľné, preskakujem' % s.id)
            continue
        bodies.append(body)

    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<tv generator-info-name="service.bigtime.setup">\n').encode('utf-8')
    _write(out_epg, head + b''.join(bodies) + b'</tv>\n')
    header = '#EXTM3U\n%s %s\n' % (MARK, ' '.join('%s=%d' % (k, v) for k, v in counts.items()))
    _write(out_m3u, (header + '\n'.join(lines) + '\n').encode('utf-8'))
    _write(state_path, json.dumps({'sig': sig, 'filtered': filtered}, indent=1).encode('utf-8'))
    say('spojené: %s' % ', '.join('%s %d' % (k, v) for k, v in counts.items()))
    return True
