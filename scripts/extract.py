"""Premiere Pro .prproj -> timeline.json for render.py.

Usage: python extract.py <project.prproj> <timeline.json> [--sequence NAME]

Reads the (gzipped XML) project, picks a sequence, and writes every video/audio track's clips with
timeline/source ranges (Premiere ticks), Motion / Crop / Ultra Key parameters, masked Mosaic effects
(including per-frame mask keyframes), and track mute state. Parameters are read by ParameterID, so the
project's UI language does not matter. Anything the renderer cannot reproduce is listed under
"warnings" and printed, so it can be reported to the user instead of silently dropped.
"""
import argparse, base64, gzip, json, struct, sys
import xml.etree.ElementTree as ET

TICKS = 254016000000  # Premiere ticks per second

ap = argparse.ArgumentParser()
ap.add_argument('project')
ap.add_argument('out')
ap.add_argument('--sequence', help='sequence name (default: the one with the most clips)')
a = ap.parse_args()

with gzip.open(a.project, 'rb') as f:
    root = ET.fromstring(f.read())
byid = {e.attrib['ObjectID']: e for e in root if 'ObjectID' in e.attrib}
byuid = {e.attrib['ObjectUID']: e for e in root if 'ObjectUID' in e.attrib}
warnings = []


def warn(msg):
    if msg not in warnings:
        warnings.append(msg)


def deref(el):
    if el is None:
        return None
    if 'ObjectRef' in el.attrib:
        return byid.get(el.attrib['ObjectRef'])
    if 'ObjectURef' in el.attrib:
        return byuid.get(el.attrib['ObjectURef'])
    return el


def ival(el, path, default=0):
    e = el.find(path)
    return int(e.text) if e is not None and e.text else default


def param_value(pe):
    cv = pe.find('CurrentValue')
    if cv is not None:
        return cv.text
    sk = pe.find('StartKeyframe')
    return sk.text.split(',')[1] if sk is not None and sk.text else None


def params_by_id(ce):
    """{ParameterID: value}; flags keyframed (animated) params"""
    out, animated = {}, []
    for p in ce.find('Component/Params'):
        pe = deref(p)
        pid = pe.find('ParameterID')
        if pid is None:
            continue
        out[pid.text] = param_value(pe)
        if pe.tag != 'ArbVideoComponentParam' and pe.find('Keyframes') is not None:
            animated.append(pid.text)
    return out, animated


def components(cti):
    chain = deref(cti.find('ComponentOwner/Components'))
    if chain is None:
        return []
    return [ce for ce in (deref(c) for c in chain.iter('Component')) if ce is not None and ce.tag != 'Component']


def decode_path(b64):
    """Premiere mask path blob -> [[x, y], ...] normalized to the clip frame (vertices only)"""
    if not b64:
        return None
    d = base64.b64decode(b64)
    if d[:4] == b'2cin':
        n, off, step = struct.unpack_from('<Q', d, 12)[0], 20, 32
    elif d[:4] == b'kcin':
        n, off, step = struct.unpack_from('<I', d, 8)[0], 16, 28
    else:
        warn(f'unknown mask path format {d[:4]!r}; that mask is ignored')
        return None
    pts = []
    for _ in range(n):
        pts.append(list(struct.unpack_from('<2f', d, off)))
        off += step
    return pts


def mask_info(m):
    info = {}
    for p in m.find('Component/Params'):
        pe = deref(p)
        pid = pe.find('ParameterID').text
        if pe.tag == 'ArbVideoComponentParam':
            kfs = pe.find('Keyframes')
            if kfs is not None and kfs.text:
                # keyframe times are in SOURCE media time (ticks)
                info['keys'] = [[int(t) / TICKS, decode_path(b)] for t, b in
                                (seg.split(',', 1) for seg in kfs.text.strip(';').split(';'))]
            else:
                sk = pe.find('StartKeyframeValue')
                info['keys'] = [[None, decode_path(sk.text if sk is not None else None)]]
        else:
            info[pid] = param_value(pe)
    return {'keys': info.get('keys', [[None, None]]), 'feather': float(info.get('7') or 0),
            'opacity': float(info.get('8') or 100), 'expansion': float(info.get('9') or 0),
            'inverted': info.get('10') == 'true'}


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def clip(tie, kind):
    cti = tie.find('ClipTrackItem')
    ti = cti.find('TrackItem')
    sub = deref(cti.find('SubClip'))
    c = deref(sub.find('Clip')).find('Clip')
    media = deref(deref(c.find('Source')).find('MediaSource/Media'))
    fp = media.find('FilePath')
    if fp is None:
        fp = media.find('ActualMediaFilePath')
    spd = c.find('PlaybackSpeed')
    item = {
        'start': ival(ti, 'Start'), 'end': ival(ti, 'End'),
        'in': ival(c, 'InPoint'), 'out': ival(c, 'OutPoint'),
        'speed': float(spd.text) if spd is not None else 1.0,
        'path': fp.text, 'file': fp.text.replace('\\', '/').split('/')[-1],
        'motion': None, 'crop': None, 'key': None, 'mosaic': None,
    }
    if item['speed'] != 1.0 and kind == 'audio':
        warn(f'audio speed change ({item["speed"]}x) on {item["file"]}: audio rendered at 1x (video speed is supported)')
    for ce in components(cti):
        mn = ce.find('MatchName')
        name = mn.text if mn is not None else ce.tag
        if kind == 'audio':
            warn('clip-level audio effects/volume found: not applied (audio is summed at 0 dB)')
            continue
        if name == 'AE.ADBE Motion':
            p, anim = params_by_id(ce)
            pos = (p.get('1') or '0.5:0.5').split(':'); anc = (p.get('6') or '0.5:0.5').split(':')
            item['motion'] = {'pos': [float(pos[0]), float(pos[1])], 'scale': num(p.get('2'), 100),
                              'anchor': [float(anc[0]), float(anc[1])], 'rotation': num(p.get('5'))}
            if num(p.get('5')):
                warn('Motion rotation used: not rendered')
            if p.get('4') == 'false' and num(p.get('3'), 100) != 100:
                warn('non-uniform Motion scale used: rendered as uniform')
            if anim:
                warn('animated Motion (keyframes) used: only the first value is rendered')
        elif name == 'AE.ADBE AECrop':
            p, anim = params_by_id(ce)
            item['crop'] = [num(p.get(k)) / 100 for k in ('1', '2', '3', '4')]  # left, top, right, bottom
            if num(p.get('6')):
                warn('crop edge feather used: not rendered')
            if anim:
                warn('animated Crop used: only the first value is rendered')
        elif name == 'AE.ADBE Ultra Key':
            p, anim = params_by_id(ce)
            col = int(p.get('3') or 0)
            # 64-bit ARGB, 16 bits per channel, 8-bit value in the high byte
            rgb = [(col >> 40) & 0xff, (col >> 24) & 0xff, (col >> 8) & 0xff]
            item['key'] = {'rgb': rgb, 'transparency': num(p.get('5')), 'tolerance': num(p.get('8')),
                           'pedestal': num(p.get('9'))}
        elif name == 'AE.ADBE Mosaic':
            vals = [param_value(deref(x)) for x in ce.find('Component/Params')]
            subs = ce.find('SubComponents')
            masks = [mask_info(deref(sc)) for sc in subs] if subs is not None else []
            item['mosaic'] = {'h': int(num(vals[0], 10)), 'v': int(num(vals[1], 10)),
                              'sharp': vals[2] == 'true', 'masks': masks}
            if any(m['expansion'] for m in masks):
                warn('mask expansion used on Mosaic: not rendered')
        elif name in ('AE.ADBE Opacity',):
            p, anim = params_by_id(ce)
            if anim or num(p.get('1'), 100) != 100:
                warn('clip opacity / fade keyframes used: not rendered')
        else:
            warn(f'unsupported video effect "{name}" ignored')
    return item


seqs = [e for e in root if e.tag == 'Sequence']


def seq_name(s):
    n = s.find('Name')
    return n.text if n is not None else '?'


def clip_count(s):
    return sum(1 for _ in s.iter('TrackItem'))


if a.sequence:
    seqs = [s for s in seqs if seq_name(s) == a.sequence] or sys.exit(f'sequence {a.sequence!r} not found')
seq = max(seqs, key=clip_count)

tl = {'ticks': TICKS, 'sequence': seq_name(seq), 'video': [], 'audio': [], 'warnings': warnings}
for tg in seq.find('TrackGroups'):
    grp = deref(tg.find('Second'))
    kind = {'VideoTrackGroup': 'video', 'AudioTrackGroup': 'audio'}.get(grp.tag)
    if not kind:
        continue
    if kind == 'video':
        tl['frame_ticks'] = ival(grp, 'TrackGroup/FrameRate')
        rect = grp.find('FrameRect')
        if rect is not None:
            x0, y0, x1, y1 = (int(v) for v in rect.text.split(','))
            tl['width'], tl['height'] = x1 - x0, y1 - y0
    for tr in grp.find('TrackGroup/Tracks'):
        t = deref(tr)
        muted = False
        if kind == 'audio':
            at = t.find('AudioTrack')
            fader = None
            for ce in (deref(c) for c in deref(at.find('ComponentOwner/Components')).iter('Component')):
                if ce is not None and ce.tag == 'AudioFader':
                    fader = ce
            if fader is not None:
                ps = [deref(p) for p in fader.iter('Param') if deref(p) is not p]
                if len(ps) > 1 and param_value(ps[1]) == 'true':      # [volume, mute]
                    muted = True
                if ps and ps[0].find('Keyframes') is not None:
                    warn('audio track volume automation used: not applied')
        else:
            muted = (t.find('.//IsMuted') is not None and t.find('.//IsMuted').text == 'true')
        items = t.find('ClipTrack/ClipItems/TrackItems')
        clips = sorted((clip(deref(i), kind) for i in items), key=lambda x: x['start']) if items is not None else []
        trans = t.find('ClipTrack/TransitionItems/TrackItems')
        if trans is not None and len(trans):
            warn(f'{len(trans)} transition(s) on a {kind} track: rendered as hard cuts')
        tl[kind].append({'muted': muted, 'clips': clips})

tl.setdefault('frame_ticks', 8475667200)
tl.setdefault('width', 1920); tl.setdefault('height', 1080)
json.dump(tl, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

fps = TICKS / tl['frame_ticks']
end = max([c['end'] for k in ('video', 'audio') for t in tl[k] for c in t['clips']] or [0]) / TICKS
print(f'sequence "{tl["sequence"]}"  {tl["width"]}x{tl["height"]} @ {fps:.3f} fps  length {end:.2f}s')
for k in ('video', 'audio'):
    for i, t in enumerate(tl[k]):
        if t['clips']:
            files = sorted({c['file'] for c in t['clips']})
            fx = sorted({f for c in t['clips'] for f in ('motion', 'crop', 'key', 'mosaic') if c.get(f)})
            print(f'  {k[0].upper()}{i + 1}: {len(t["clips"])} clips{"  MUTED" if t["muted"] else ""}'
                  f'  fx={",".join(fx) or "-"}  files={", ".join(files)}')
for w in warnings:
    print('WARNING:', w)
