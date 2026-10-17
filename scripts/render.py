"""timeline.json (from extract.py) -> mp4, rendered with ffmpeg.

Usage:
  python render.py <timeline.json> <media_dir> <work_dir> <out.mp4> [--frames A:B] [--jobs N]
                   [--crf 20] [--preset fast] [--key-threshold 42] [--key-softness 24] [--despill 6]

How it works
  * The sequence is cut into segments where the set of clips on every video track is constant.
    Each segment is one ffmpeg run, cached in work_dir under a name derived from what it shows (sources,
    source times, effects, encoder settings), so after a partial edit only changed segments re-render. Then all
    segments are joined with the concat demuxer (no re-encode) and muxed with the audio.
  * Video tracks are composited bottom (V1) to top. Per clip: Crop -> Motion (scale/position/anchor)
    -> green/blue key (approximation of Ultra Key) -> overlay.
  * Stills (png/jpg) loop; runs of 1-frame clips showing the same source frame become a freeze.
  * Masked Mosaic on the bottom layer is done in numpy: blocks span the union bbox of all masks
    (clipped to the frame), which matches Premiere's output; mask keyframes are interpolated.
  * Audio: every unmuted track is summed sample-accurately at 0 dB (48 kHz stereo).
--frames A:B renders only the segments overlapping frames [A, B) and stops (for quick tests).
"""
import argparse, glob, hashlib, json, os, shutil, subprocess, sys, wave
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument('timeline'); ap.add_argument('media'); ap.add_argument('work'); ap.add_argument('out')
ap.add_argument('--frames'); ap.add_argument('--jobs', type=int, default=4)
ap.add_argument('--crf', default='20'); ap.add_argument('--preset', default='fast')
ap.add_argument('--key-threshold', type=float, default=42, help='screen-colour dominance where alpha reaches 0')
ap.add_argument('--key-softness', type=float, default=24, help='ramp width below the threshold')
ap.add_argument('--despill', type=float, default=6, help='allowed excess of the screen channel after despill')
a = ap.parse_args()

tl = json.load(open(a.timeline, encoding='utf-8'))
T, F = tl['ticks'], tl['frame_ticks']
W, H = tl['width'], tl['height']
FPS = T / F
RATE = '30000/1001' if abs(FPS - 29.97) < 0.01 else '24000/1001' if abs(FPS - 23.976) < 0.01 else \
       '60000/1001' if abs(FPS - 59.94) < 0.01 else str(round(FPS))
SETPTS = f'setpts=N/({RATE})/TB'
os.makedirs(a.work, exist_ok=True)

fr = lambda ticks: round(ticks / F)
is_still = lambda name: name.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.psd'))


def media(c):
    p = os.path.join(a.media, c['file'])
    if not os.path.exists(p):
        p = c['path']                     # fall back to the path stored in the project
    if not os.path.exists(p):
        sys.exit(f'media not found: {c["file"]} (looked in {a.media} and {c["path"]})')
    return p


_size = {}
def src_size(c):
    p = media(c)
    if p not in _size:
        if is_still(p):
            _size[p] = Image.open(p).size
        else:
            r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                'stream=width,height', '-of', 'csv=p=0', p], capture_output=True, text=True)
            w, h = r.stdout.strip().split(',')[:2]
            _size[p] = (int(w), int(h))
    return _size[p]


_rate = {}
def needs_fps(c):
    """True when the source is not CFR at the sequence rate (phone recordings: 60 fps / VFR). Those get an
    fps filter so one output frame = 1/FPS of source time; CFR sources at the sequence rate are left alone
    (the frame-index setpts already keeps them exact, and an fps filter could drop/duplicate jittery frames)."""
    p = media(c)
    if p not in _rate:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=r_frame_rate,avg_frame_rate', '-of', 'csv=p=0', p], capture_output=True, text=True)
        rates = []
        for v in r.stdout.strip().split(',')[:2]:
            num, _, den = v.partition('/')
            rates.append(float(num) / float(den or 1) if float(den or 1) else 0)
        _rate[p] = any(abs(x - FPS) > 0.5 for x in rates)
    return _rate[p]


tracks =[[dict(c, fs=fr(c['start']), fe=fr(c['end'])) for c in t['clips']] for t in tl['video'] if not t['muted']]
N = max([c['fe'] for t in tracks for c in t] or [0])


def at(clips, f):
    for c in clips:
        if c['fs'] <= f < c['fe']:
            return c
    return None


def src_time(c, f):
    """source seconds shown at sequence frame f (playback speed applied: 1.5 = fast forward)"""
    return (c['in'] + (f - c['fs']) * F * (c.get('speed') or 1.0)) / T


def ident(c):
    if c is None:
        return None
    if c['fe'] - c['fs'] == 1:                 # 1-frame clips of the same source frame = freeze run
        return ('freeze', c['file'], c['in'])
    return ('clip', c['file'], c['start'])


segs, f = [], 0
while f < N:
    key = tuple(ident(at(t, f)) for t in tracks)
    g = f + 1
    while g < N and tuple(ident(at(t, g)) for t in tracks) == key:
        g += 1
    segs.append({'fs': f, 'fe': g, 'layers': [at(t, f) for t in tracks]})
    f = g


def seg_name(seg):
    """Cache name from what the segment SHOWS (sources, source times, effects, encoder settings), not from
    its position: after cutting or inserting a clip, unchanged segments are reused and changed ones re-rendered."""
    layers = []
    for c in seg['layers']:
        if c is None:
            layers.append(None); continue
        if is_still(c['file']):
            src = 'still'
        elif c['fe'] - c['fs'] == 1:
            src = ('freeze', c['in'])
        else:
            src = round(src_time(c, seg['fs']), 4)
        layers.append([media(c), src, c.get('speed'), c.get('motion'), c.get('crop'), c.get('key'), c.get('mosaic')])
    spec = ['v2', seg['fe'] - seg['fs'], W, H, RATE, a.crf, a.preset, a.key_threshold, a.key_softness, a.despill, layers]
    return 'seg_' + hashlib.sha1(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


for s_ in segs:
    s_['name'] = seg_name(s_)
print(f'{len(segs)} segments, {N} frames, {W}x{H} @ {RATE}')


# ------------------------------------------------------------------ mosaic (numpy)
def interp_path(keys, t):
    if not keys or keys[0][0] is None or len(keys) == 1:
        return keys[0][1] if keys else None
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, p0), (t1, p1) in zip(keys, keys[1:]):
        if t0 <= t < t1:
            if not p0 or not p1 or len(p0) != len(p1):
                return p0
            k = (t - t0) / (t1 - t0)
            return [[x0 + (x1 - x0) * k, y0 + (y1 - y0) * k] for (x0, y0), (x1, y1) in zip(p0, p1)]
    return keys[-1][1]


def mosaic_frame(img, mz, t):
    """Premiere-style masked Mosaic on an HxWx3 frame (any 3-channel space)."""
    live = [(m, p) for m, p in ((m, interp_path(m['keys'], t)) for m in mz['masks']) if p and len(p) >= 3]
    if not live:
        return img
    polys = [[(x * W, y * H) for x, y in p] for _, p in live]
    if mz.get('block_px'):
        return _mosaic_fixed(img, mz['block_px'], live, polys)
    xs = [x for p in polys for x, _ in p]; ys = [y for p in polys for _, y in p]
    x0, y0, x1, y1 = max(0, min(xs)), max(0, min(ys)), min(W, max(xs)), min(H, max(ys))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return img
    f = img.astype(np.float32)
    mos = f.copy()
    bx = np.round(np.linspace(x0, x1, max(1, mz['h']) + 1)).astype(int)
    by = np.round(np.linspace(y0, y1, max(1, mz['v']) + 1)).astype(int)
    for r0, r1 in zip(by, by[1:]):
        for c0, c1 in zip(bx, bx[1:]):
            if r1 > r0 and c1 > c0:
                blk = f[r0:r1, c0:c1]
                mos[r0:r1, c0:c1] = blk[(r1 - r0) // 2, (c1 - c0) // 2] if mz['sharp'] else blk.reshape(-1, 3).mean(0)
    alpha = np.zeros((H, W), np.float32)
    for (m, _), poly in zip(live, polys):
        layer = Image.new('L', (W, H), 0)
        ImageDraw.Draw(layer).polygon(poly, fill=round(255 * m['opacity'] / 100))
        if m['feather'] > 0:
            layer = layer.filter(ImageFilter.GaussianBlur(m['feather'] / 2))
        la = np.asarray(layer, np.float32) / 255
        alpha = np.maximum(alpha, 1 - la if m['inverted'] else la)
    alpha = alpha[:, :, None]
    return np.clip(f * (1 - alpha) + mos * alpha + 0.5, 0, 255).astype(np.uint8)


def _mosaic_fixed(img, bpx, live, polys):
    """Mosaic with fixed-size blocks per mask (not a Premiere mode): block_px x block_px squares
    inside each mask's own bbox, so thin strips are obscured as well as large areas."""
    out = img.astype(np.float32)
    for (m, _), poly in zip(live, polys):
        xs = [x for x, _ in poly]; ys = [y for _, y in poly]
        x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
        x1, y1 = min(W, int(np.ceil(max(xs)))), min(H, int(np.ceil(max(ys))))
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue
        reg = out[y0:y1, x0:x1].copy()
        for r0 in range(0, y1 - y0, bpx):
            for c0 in range(0, x1 - x0, bpx):
                blk = reg[r0:r0 + bpx, c0:c0 + bpx]
                blk[...] = blk.reshape(-1, 3).mean(0)
        layer = Image.new('L', (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(layer).polygon([(x - x0, y - y0) for x, y in poly], fill=round(255 * m['opacity'] / 100))
        if m['feather'] > 0:
            layer = layer.filter(ImageFilter.GaussianBlur(m['feather'] / 2))
        al = np.asarray(layer, np.float32)[:, :, None] / 255
        if m['inverted']:
            al = 1 - al
        out[y0:y1, x0:x1] = out[y0:y1, x0:x1] * (1 - al) + reg * al
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def mosaic_feed(seg, c, proc, n):
    """decode the clip as planar yuv444p (no matrix conversion), mosaic each frame, pipe to the encoder"""
    dec = subprocess.Popen(['ffmpeg', '-v', 'error', '-ss', f'{src_time(c, seg["fs"]) - 0.25 / FPS:.6f}',
                            '-i', media(c), '-frames:v', str(n), '-vf', f'scale={W}:{H}',
                            '-f', 'rawvideo', '-pix_fmt', 'yuv444p', '-'], stdout=subprocess.PIPE)
    size, last = W * H * 3, None
    try:
        for i in range(n):
            buf = dec.stdout.read(size)
            if len(buf) == size:
                last = np.frombuffer(buf, np.uint8).reshape(3, H, W).transpose(1, 2, 0)
            out = mosaic_frame(last, c['mosaic'], src_time(c, seg['fs'] + i))
            proc.stdin.write(np.ascontiguousarray(out.transpose(2, 0, 1)).tobytes())
    finally:
        proc.stdin.close(); dec.stdout.close(); dec.wait()


# ------------------------------------------------------------------ layer geometry / key
def key_filter(k):
    r, g, b = k['rgb']
    s = 'g' if g >= b else 'b'                 # screen channel (green or blue screen)
    o1, o2 = ('r', 'b') if s == 'g' else ('r', 'g')
    dom = f"({s}(X,Y)-({o1}(X,Y)+{o2}(X,Y))/2)"
    ch = {c: f"'{c}(X,Y)'" for c in 'rgb'}
    ch[s] = f"'min({s}(X,Y),max({o1}(X,Y),{o2}(X,Y))+{a.despill})'"
    return (f"format=gbrap,geq=r={ch['r']}:g={ch['g']}:b={ch['b']}:"
            f"a='255*clip(({a.key_threshold}-{dom})/{a.key_softness},0,1)'")


def is_fullframe(c):
    sw, sh = src_size(c)
    mo = c.get('motion')
    default_motion = mo is None or (abs(mo['scale'] - 100) < 1e-6 and all(abs(v - 0.5) < 1e-6 for v in mo['pos'] + mo['anchor']))
    return (sw, sh) == (W, H) and default_motion and not any(c.get('crop') or [0]) and not c.get('key')


def layer_chain(c):
    """filter chain + overlay x,y placing clip c in the sequence frame"""
    sw, sh = src_size(c)
    mo = c.get('motion') or {'pos': [0.5, 0.5], 'scale': 100, 'anchor': [0.5, 0.5]}
    s = mo['scale'] / 100
    l, t, r, b = c.get('crop') or [0, 0, 0, 0]
    cx, cy = round(sw * l), round(sh * t)
    cw, chh = max(2, round(sw * (1 - l - r))), max(2, round(sh * (1 - t - b)))
    ow, oh = max(2, round(cw * s / 2) * 2), max(2, round(chh * s / 2) * 2)
    x = round(mo['pos'][0] * W - mo['anchor'][0] * sw * s + cx * s)
    y = round(mo['pos'][1] * H - mo['anchor'][1] * sh * s + cy * s)
    chain = [f'crop={cw}:{chh}:{cx}:{cy}'] if (cw, chh) != (sw, sh) else []
    if (ow, oh) != (cw, chh):
        chain.append(f'scale={ow}:{oh}:flags=bicubic')
    chain.append(key_filter(c['key']) if c.get('key') else 'format=yuva420p')
    return ','.join(chain), x, y


def source_input(c, seg, n):
    """ffmpeg input args + the filter prefix that yields n frames of clip c for this segment"""
    if is_still(c['file']):
        return ['-loop', '1', '-framerate', RATE, '-i', media(c)], f'format=yuva444p,{SETPTS}'
    if c['fe'] - c['fs'] == 1:
        return (['-ss', f'{c["in"] / T - 0.25 / FPS:.6f}', '-i', media(c)],
                f'trim=end_frame=1,loop=loop={n}:size=1,{SETPTS}')
    sp = c.get('speed') or 1.0
    if abs(sp - 1.0) > 1e-6:                     # speed change: retime, then resample to the sequence rate
        return (['-ss', f'{src_time(c, seg["fs"]):.6f}', '-i', media(c)],
                f'setpts=(PTS-STARTPTS)/{sp:.6f},fps={RATE},{SETPTS}')
    if needs_fps(c):
        return (['-ss', f'{src_time(c, seg["fs"]):.6f}', '-i', media(c)],
                f'setpts=PTS-STARTPTS,fps={RATE},{SETPTS}')
    return ['-ss', f'{src_time(c, seg["fs"]) - 0.25 / FPS:.6f}', '-i', media(c)], SETPTS


def render(i, seg):
    out = os.path.join(a.work, seg['name'] + '.mp4')
    if os.path.exists(out):
        return out
    n = seg['fe'] - seg['fs']
    args, fc = ['ffmpeg', '-v', 'error', '-y'], []
    layers = [c for c in seg['layers'] if c is not None]
    mosaic_clip = None
    base = layers[0] if layers else None
    if base is not None and base.get('mosaic') and not is_still(base['file']) and base['fe'] - base['fs'] > 1 \
            and is_fullframe(base):
        mosaic_clip = base
        args += ['-f', 'rawvideo', '-pix_fmt', 'yuv444p', '-s', f'{W}x{H}', '-r', RATE, '-i', '-']
        fc.append(f'[0:v]{SETPTS}[l0]')
        layers = layers[1:]
    elif base is not None and is_fullframe(base):
        ia, pre = source_input(base, seg, n)
        args += ia
        fc.append(f'[0:v]{pre},scale={W}:{H},format=yuv420p[l0]')
        layers = layers[1:]
    else:
        args += ['-f', 'lavfi', '-i', f'color=black:s={W}x{H}:r={RATE}']
        fc.append(f'[0:v]{SETPTS}[l0]')
    if base is not None and base.get('mosaic') and mosaic_clip is None:
        print(f'  note: segment {i}: Mosaic only supported on a full-frame bottom layer; skipped')
    cur, idx = 'l0', 1
    for c in layers:
        ia, pre = source_input(c, seg, n)
        args += ia
        chain, x, y = layer_chain(c)
        fc.append(f'[{idx}:v]{pre},{chain}[f{idx}];[{cur}][f{idx}]overlay={x}:{y}:eof_action=repeat[l{idx}]')
        cur, idx = f'l{idx}', idx + 1
    fc.append(f'[{cur}]format=yuv420p[vout]')
    fcfile = os.path.join(a.work, seg['name'] + '.fc')
    open(fcfile, 'w', encoding='utf-8').write(';'.join(fc))
    args += ['-/filter_complex', fcfile, '-map', '[vout]', '-frames:v', str(n), '-an', '-r', RATE,
             '-c:v', 'libx264', '-preset', a.preset, '-crf', a.crf, '-pix_fmt', 'yuv420p',
             '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
             '-video_track_timescale', str(round(FPS * 1001)) if RATE.endswith('/1001') else str(round(FPS) * 1000),
             out + '.tmp.mp4']
    logf = open(out + '.log', 'w+', encoding='utf-8', errors='replace')
    if mosaic_clip is not None:
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=logf)
        mosaic_feed(seg, mosaic_clip, p, n)
        rc = p.wait()
    else:
        rc = subprocess.run(args, stderr=logf).returncode
    if rc:
        logf.seek(0)
        raise RuntimeError(f'segment {i} (frames {seg["fs"]}-{seg["fe"]}) failed: ' + logf.read()[-2000:])
    logf.close(); os.remove(out + '.log')
    os.replace(out + '.tmp.mp4', out)
    return out


todo = list(enumerate(segs))
if a.frames:
    lo, hi = (int(x) for x in a.frames.split(':'))
    todo = [(i, s) for i, s in todo if s['fs'] < hi and s['fe'] > lo]
done = 0
with ThreadPoolExecutor(max_workers=a.jobs) as ex:
    for path in ex.map(lambda p: render(*p), todo):
        done += 1
        if a.frames or done % 10 == 0 or done == len(todo):
            print(f'  video {done}/{len(todo)}  {path if a.frames else ""}', flush=True)
if a.frames:
    for i, s in todo:
        print(f'  segment {i}: {s["name"]}.mp4 = sequence frames {s["fs"]}..{s["fe"] - 1}')
    sys.exit(0)

# ------------------------------------------------------------------ audio
SR = 48000
pcm_cache = {}
def pcm(c):
    p = media(c)
    if p not in pcm_cache:
        wav = os.path.join(a.work, f'audio_{len(pcm_cache):02d}.wav')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', p, '-vn', '-ac', '2', '-ar', str(SR),
                        '-c:a', 'pcm_s16le', wav], check=True)
        with wave.open(wav) as w:
            pcm_cache[p] = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, 2)
    return pcm_cache[p]

total = round(N * F / T * SR)
mix = np.zeros((total, 2), np.float32)
for track in tl['audio']:
    if track['muted']:
        continue
    for c in track['clips']:
        s0, s1 = round(c['start'] / T * SR), min(total, round(c['end'] / T * SR))
        a0 = round(c['in'] / T * SR)
        src = pcm(c)[a0:a0 + max(0, s1 - s0)]
        mix[s0:s0 + len(src)] += src
audio_wav = os.path.join(a.work, 'mix.wav')
with wave.open(audio_wav, 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes(np.clip(mix, -32768, 32767).astype(np.int16).tobytes())
print('audio done')

lst = os.path.join(a.work, 'list.txt')
with open(lst, 'w', encoding='utf-8') as f:
    for s_ in segs:   # concat demuxer resolves relative paths against the list file: use absolute
        f.write(f"file '{os.path.abspath(os.path.join(a.work, s_['name'] + '.mp4')).replace(os.sep, '/')}'\n")
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-i', audio_wav,
                '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k',
                '-movflags', '+faststart', a.out], check=True)
print('wrote', a.out)

# drop cached segments the current timeline no longer uses (left over from earlier versions of the edit)
used = {s_['name'] for s_ in segs}
stale = [p for p in glob.glob(os.path.join(a.work, 'seg*.mp4')) + glob.glob(os.path.join(a.work, 'seg*.fc'))
         if os.path.splitext(os.path.basename(p))[0] not in used]
for p in stale:
    os.remove(p)
if stale:
    print(f'removed {len(stale)} stale segment files')
