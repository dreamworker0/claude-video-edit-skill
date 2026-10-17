"""Edit-from-raw, step 3: edl.json -> timeline.json for render.py (screen + keyed camera + voice).

Usage:
  python scratch_timeline.py <work_dir> <media_dir> [--decisions decisions.json] [--mosaic masks.json]
         [--cam-scale 35] [--cam-y 0.825] [--cam-x 0.873,0.127] [--crop 0.1,0.1,0.1,0] [--no-auto-side]

* Background V1 = screen recording at camera_time + lag, or a title image for clips listed in
  decisions["title"] = {"image": "title.png", "ranges": [["<cam stem>", from_s, to_s(, "other.png")], ...]}
  (4th element = a different image for that range), or B-roll from decisions["broll"] (see below).
* V2 = camera: crop (l,t,r,b fractions), scale %, centre at (x, cam-y). The camera starts bottom-right and
  moves to the other corner only when the screen under it is busy and the other corner is clearly emptier
  (with hysteresis, so it does not jump back and forth). --no-auto-side keeps it on the right.
* A1 = camera audio.  --mosaic: {clip_index: mosaic dict} from privacy_masks.py.
"""
import argparse, json, os, subprocess
from concurrent.futures import ThreadPoolExecutor
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('work'); ap.add_argument('media')
ap.add_argument('--decisions'); ap.add_argument('--mosaic')
ap.add_argument('--cam-scale', type=float, default=35); ap.add_argument('--cam-y', type=float, default=0.825)
ap.add_argument('--cam-x', default='0.873,0.127'); ap.add_argument('--crop', default='auto', help="l,t,r,b fractions, or 'auto' (sides 0.1, top just above the head)")
ap.add_argument('--no-auto-side', action='store_true')
a = ap.parse_args()

T, F = 254016000000, 8475667200          # ticks/s, ticks per 1001/30000 s frame
FPS = 30000 / 1001
W, H = 1920, 1080
clips = json.load(open(os.path.join(a.work, 'edl.json'), encoding='utf-8'))
dec = json.load(open(a.decisions, encoding='utf-8')) if a.decisions else {}
mosaic = json.load(open(a.mosaic, encoding='utf-8')) if a.mosaic else {}
title = dec.get('title', {})
stem = lambda f: os.path.splitext(os.path.basename(f))[0]
xr, xl = (float(v) for v in a.cam_x.split(','))


def auto_crop():
    """sides 10%; top = highest point of the person over ~12 frames minus a margin, so the head is never cut
    (green above the head is keyed away anyway, so a small top crop costs nothing)"""
    cams = sorted({c['cam'] for c in clips})
    tops = []
    for cam in cams:
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                                    os.path.join(a.media, cam)], capture_output=True, text=True).stdout or 0)
        for k in range(1, 7):
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{dur * k / 7:.2f}', '-i', os.path.join(a.media, cam),
                                  '-frames:v', '1', '-vf', 'scale=320:180', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                                 capture_output=True).stdout
            if len(raw) != 320 * 180 * 3:
                continue
            x = np.frombuffer(raw, np.uint8).reshape(180, 320, 3).astype(int)
            fg = ((x[..., 1] - np.maximum(x[..., 0], x[..., 2])) < 25)[:, 64:256]   # not green, central 60%
            rows = np.nonzero(fg.mean(1) > 0.04)[0]
            if len(rows):
                tops.append(rows.min() / 180)
    top = max(0.0, min(tops) - 0.03) if tops else 0.0
    return [0.1, float(round(min(top, 0.1), 3)), 0.1, 0.0]

crop = auto_crop() if a.crop == 'auto' else [float(v) for v in a.crop.split(',')]
print('camera crop (l,t,r,b):', crop)

def use_title(c):
    return any(stem(c['cam']) == r[0] and r[1] <= c['s'] < r[2] for r in title.get('ranges', []))

# camera boxes in sequence pixels (for the busy-corner test)
s = a.cam_scale / 100
bw, bh = W * (1 - crop[0] - crop[2]) * s, H * (1 - crop[1] - crop[3]) * s
cy = a.cam_y * H - H * s / 2 + crop[1] * H * s
RIGHT = (int(xr * W - bw / 2), int(cy), min(W, int(xr * W + bw / 2)), min(H, int(cy + bh)))
LEFT = (max(0, int(xl * W - bw / 2)), int(cy), int(xl * W + bw / 2), min(H, int(cy + bh)))

def busy(c):
    vals = []
    for k in (0.15, 0.5, 0.85):
        t = c['s'] + c['lag'] + (c['e'] - c['s']) * k
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', os.path.join(a.media, c['scr']),
                              '-frames:v', '1', '-vf', f'scale={W // 2}:{H // 2},format=gray', '-f', 'rawvideo', '-'],
                             capture_output=True).stdout
        g = np.frombuffer(raw, np.uint8).reshape(H // 2, W // 2).astype(np.float32)
        def score(b):
            r = g[b[1] // 2:b[3] // 2, b[0] // 2:b[2] // 2]
            return float(np.abs(np.diff(r, axis=1)).mean() + np.abs(np.diff(r, axis=0)).mean())
        vals.append((score(RIGHT), score(LEFT)))
    return np.array(vals).max(0)

if a.no_auto_side:
    sides = ['R'] * len(clips)
else:
    with ThreadPoolExecutor(8) as ex:
        scores = list(ex.map(lambda c: None if use_title(c) else busy(c), clips))
    side, sides = 'R', []
    for sc in scores:
        if sc is not None:
            cur, other = (sc[0], sc[1]) if side == 'R' else (sc[1], sc[0])
            if cur > 2.0 and other < cur * 0.6:
                side = 'L' if side == 'R' else 'R'
        sides.append(side)

# key colour: median of strongly green (or blue) pixels in a camera frame
def key_rgb(cam):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '30', '-i', os.path.join(a.media, cam), '-frames:v', '1',
                          '-vf', 'scale=320:180', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True).stdout
    x = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(int)
    g = x[(x[:, 1] - np.maximum(x[:, 0], x[:, 2])) > 30]
    b = x[(x[:, 2] - np.maximum(x[:, 0], x[:, 1])) > 30]
    pick = g if len(g) >= len(b) else b
    return [int(v) for v in np.median(pick, 0)] if len(pick) else [0, 160, 60]
KEY = {'rgb': key_rgb(clips[0]['cam']), 'transparency': 45, 'tolerance': 59, 'pedestal': 40}

def item(file, st, en, src_in, **kw):
    d = {'start': st, 'end': en, 'in': src_in, 'out': src_in + en - st, 'speed': 1.0, 'path': file, 'file': file,
         'motion': None, 'crop': None, 'key': None, 'mosaic': None}
    d.update(kw); return d

_dims = {}
def dims(f):
    if f not in _dims:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                            '-of', 'csv=p=0', os.path.join(a.media, f)], capture_output=True, text=True)
        _dims[f] = tuple(int(v) for v in r.stdout.strip().split(',')[:2])
    return _dims[f]

# B-roll: decisions["broll"] = [{"cam": "<cam stem>", "from": s, "to": s, "file": "phone.mp4", "in": s, "speed": 1.5,
#   "pos": [0.5, 0.5]}] -> while the camera plays [from, to) the background shows `file` (fit to the frame,
#   letterboxed on black). The B-roll plays continuously from "in" across cuts inside the window, because
#   separately recorded demos are not synced to the voice.
broll = dec.get('broll', [])
played = [0.0] * len(broll)
def broll_item(b, k, st, en):
    sw, sh = dims(b['file'])
    sc = min(W / sw, H / sh) * 100
    sp = b.get('speed', 1.0)                     # e.g. 1.5 to fast-forward a slow phone demo
    src = b.get('in', 0.0) + played[k]
    played[k] += (en - st) / T * sp
    it = item(b['file'], st, en, round(src * T),
              motion={'pos': b.get('pos', [0.5, 0.5]), 'scale': b.get('scale', sc), 'anchor': [0.5, 0.5], 'rotation': 0})
    it['speed'] = sp
    return it

def title_image(c):
    for r in title.get('ranges', []):
        if stem(c['cam']) == r[0] and r[1] <= c['s'] < r[2]:
            return r[3] if len(r) > 3 else title['image']

v1, v2, a1, pos = [], [], [], 0
for i, c in enumerate(clips):
    n = round((c['e'] - c['s']) * FPS)
    st, en = pos * F, (pos + n) * F
    cin, sin = round(c['s'] * T), round((c['s'] + c['lag']) * T)
    if use_title(c):
        v1.append(item(title_image(c), st, en, 0))
    else:
        # split the clip's background at B-roll window edges (frame-accurate, in camera time)
        cuts = {0, n}
        wins = [(k, b) for k, b in enumerate(broll) if b['cam'] == stem(c['cam']) and b['from'] < c['e'] and b['to'] > c['s']]
        for _, b in wins:
            for t_ in (b['from'], b['to']):
                if c['s'] < t_ < c['e']:
                    cuts.add(round((t_ - c['s']) * FPS))
        cuts = sorted(cuts)
        for k0, k1 in zip(cuts, cuts[1:]):
            if k1 <= k0:
                continue
            p_st, p_en = (pos + k0) * F, (pos + k1) * F
            mid = c['s'] + (k0 + k1) / 2 / FPS
            hit = next(((k, b) for k, b in wins if b['from'] <= mid < b['to']), None)
            if hit:
                v1.append(broll_item(hit[1], hit[0], p_st, p_en))
            else:
                v1.append(item(c['scr'], p_st, p_en, sin + k0 * F, mosaic=mosaic.get(str(i))))
    v2.append(item(c['cam'], st, en, cin, crop=crop, key=KEY,
                   motion={'pos': [xr if sides[i] == 'R' else xl, a.cam_y], 'scale': a.cam_scale,
                           'anchor': [0.5, 0.5], 'rotation': 0}))
    a1.append(item(c['cam'], st, en, cin))
    pos += n

# cover images over the first / last N seconds: decisions["intro"] / ["outro"] = {"image": "cover.png", "seconds": 5}.
# The camera and voice keep playing on top (the user's own style); only the background is replaced.
def cover(spec, lo, hi):
    out = []
    for c in v1:
        a_, b_ = c['start'], c['end']
        if b_ <= lo or a_ >= hi:
            out.append(c); continue
        if a_ < lo:
            out.append(dict(c, end=lo, out=c['in'] + lo - a_))
        out.append(item(spec['image'], max(a_, lo), min(b_, hi), 0))
        if b_ > hi:
            off = hi - a_
            out.append(dict(c, start=hi, **{'in': c['in'] + round(off * (c.get('speed') or 1.0))}))
    return out
END = pos * F
for key, lo, hi in (('intro', 0, None), ('outro', None, END)):
    spec = dec.get(key)
    if spec:
        nfr = round(spec.get('seconds', 5) * FPS) * F
        v1 = cover(spec, lo if lo is not None else END - nfr, hi if hi is not None else nfr)

tl = {'ticks': T, 'frame_ticks': F, 'width': W, 'height': H, 'sequence': 'edit from raw',
      'video': [{'muted': False, 'clips': v1}, {'muted': False, 'clips': v2}],
      'audio': [{'muted': False, 'clips': a1}], 'warnings': []}
json.dump(tl, open(os.path.join(a.work, 'timeline.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(sides, open(os.path.join(a.work, 'sides.json'), 'w'))
print(f'{len(clips)} clips, {pos} frames ({pos / FPS:.1f}s); camera right {sides.count("R")}, left {sides.count("L")}; '
      f'title-card clips {sum(map(use_title, clips))}; key colour {KEY["rgb"]}  -> timeline.json')
