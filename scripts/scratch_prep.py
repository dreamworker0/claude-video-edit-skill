"""Edit-from-raw, step 1: pair camera + screen recordings, sync them, split speech into units and
transcribe every unit on its own (so each word lands in the right unit).

Usage:
  python scratch_prep.py <media_dir> <work_dir> --model <ggml-whisper.bin> [--pair CAM.mp4=SCREEN.mp4 ...]
                         [--lang ko] [--min-silence 0.25] [--jobs 5]

Outputs in work_dir:
  project.json   pairs (camera file, screen file, lag: screen_time = camera_time + lag), durations
  units.json     per camera file: [{s, e, t (text), wt [[word, start_s], ...]}]
  ut_<cam>.txt   human-readable unit list:  idx start-end gap | text   <- read these to decide the cut
Audio is taken from the camera file (it is the voice track that gets used). Re-running skips finished stages.
"""
import argparse, glob, json, os, subprocess, wave
from concurrent.futures import ThreadPoolExecutor
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('media'); ap.add_argument('work')
ap.add_argument('--model', required=True)
ap.add_argument('--pair', action='append', default=[], help='CAMERA=SCREEN file names (skip auto pairing)')
ap.add_argument('--lang', default='ko')
ap.add_argument('--min-silence', type=float, default=0.25)
ap.add_argument('--jobs', type=int, default=5)
a = ap.parse_args()
os.makedirs(a.work, exist_ok=True)
SR = 16000
stem = lambda f: os.path.splitext(os.path.basename(f))[0]


def run(cmd):
    subprocess.run(cmd, check=True)


def probe(f):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:format_tags=creation_time',
                        '-of', 'json', f], capture_output=True, text=True)
    d = json.loads(r.stdout)['format']
    return float(d.get('duration', 0)), d.get('tags', {}).get('creation_time')


def green_score(f, dur):
    """fraction of strongly green pixels in a mid frame: high = green-screen camera"""
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{dur / 2:.2f}', '-i', f, '-frames:v', '1',
                          '-vf', 'scale=320:180', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True).stdout
    x = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(int)
    return float(((x[:, 1] - np.maximum(x[:, 0], x[:, 2])) > 30).mean())


def wav(f):
    out = os.path.join(a.work, 'wav', stem(f) + '.wav')
    if not os.path.exists(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        run(['ffmpeg', '-v', 'error', '-y', '-i', f, '-vn', '-ac', '1', '-ar', str(SR), '-c:a', 'pcm_s16le', out])
    with wave.open(out) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16)


def whisper(src_wav, dest_json):
    if os.path.exists(dest_json) and os.path.getsize(dest_json) > 0:
        return
    # filter option values: quote the path and escape ':' (Windows drive letters, spaces in names)
    esc = lambda p: "'" + os.path.abspath(p).replace('\\', '/').replace(':', '\\:') + "'"
    run(['ffmpeg', '-v', 'error', '-y', '-i', src_wav, '-af',
         f'whisper=model={esc(a.model)}:language={a.lang}:queue=30:max_len=1:destination={esc(dest_json)}:format=json',
         '-f', 'null', '-'])


# ---------------------------------------------------------------- 1. pairs + sync
pj = os.path.join(a.work, 'project.json')
if os.path.exists(pj):
    proj = json.load(open(pj, encoding='utf-8'))
else:
    vids = sorted(f for f in glob.glob(os.path.join(a.media, '*.mp4')) if not os.path.basename(f).startswith('Claude'))
    info = {f: probe(f) for f in vids}
    pairs = []
    if a.pair:
        for p in a.pair:
            c, s = p.split('=')
            pairs.append((os.path.join(a.media, c), os.path.join(a.media, s)))
    else:
        # recordings started together have near-identical durations; the greener one is the camera
        left = sorted(vids, key=lambda f: info[f][0])
        used = set()
        for f in left:
            if f in used:
                continue
            best = min((g for g in left if g != f and g not in used), key=lambda g: abs(info[g][0] - info[f][0]), default=None)
            if best is None or abs(info[best][0] - info[f][0]) > 5:
                continue
            used |= {f, best}
            cam, scr = (f, best) if green_score(f, info[f][0]) >= green_score(best, info[best][0]) else (best, f)
            pairs.append((cam, scr))
    proj = {'pairs': []}
    for cam, scr in pairs:
        x, y = wav(cam).astype(np.float32), wav(scr).astype(np.float32)
        # measure the lag in several 20 s windows; a consistent lag is what proves the sync. The correlation
        # value itself can be low (0.6-0.7) when the two files got different mic processing / levels.
        dur_s = min(len(x), len(y)) / SR
        lags, corrs = [], []
        for frac in (0.15, 0.4, 0.65, 0.85):
            st = int(dur_s * frac * SR)
            seg = x[st:st + 20 * SR]
            lo = max(0, st - 8 * SR)
            ref = y[lo:st + 28 * SR]
            if len(seg) < 5 * SR or len(ref) <= len(seg) or seg.std() < 50:
                continue
            n = 1 << int(np.ceil(np.log2(len(seg) + len(ref))))
            c = np.fft.irfft(np.fft.rfft(ref, n) * np.conj(np.fft.rfft(seg, n)), n)[:len(ref) - len(seg)]
            k = int(np.argmax(c))
            yy = ref[k:k + len(seg)]
            lags.append(round((lo + k - st) / SR, 3))
            corrs.append(float(np.corrcoef(seg, yy)[0, 1]) if yy.std() > 0 else 0.0)
        lag = float(np.median(lags)) if lags else 0.0
        corr = float(np.median(corrs)) if corrs else 0.0
        agree = sum(abs(l - lag) <= 0.05 for l in lags)        # windows that point at the same lag
        proj['pairs'].append({'cam': os.path.basename(cam), 'scr': os.path.basename(scr), 'lag': round(lag, 3),
                              'corr': round(corr, 3), 'lag_agree': f'{agree}/{len(lags)}', 'dur': round(info[cam][0], 2)})
    # order by recording time (file names from OBS sort chronologically)
    proj['pairs'].sort(key=lambda p: p['cam'])
    json.dump(proj, open(pj, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
for p in proj['pairs']:
    ag, tot = (int(v) for v in p.get('lag_agree', '1/1').split('/'))
    ok = 'OK' if tot and ag >= max(2, tot - 1) else 'CHECK SYNC (windows disagree - try --pair or inspect)'
    print(f"pair  cam={p['cam']}  screen={p['scr']}  screen_time = cam_time {p['lag']:+.3f}s  "
          f"(corr {p['corr']}, {ag}/{tot} windows agree) {ok}")

# ---------------------------------------------------------------- 2. speech units from the energy envelope
uj = os.path.join(a.work, 'units.json')
units = json.load(open(uj, encoding='utf-8')) if os.path.exists(uj) else {}
todo = [p for p in proj['pairs'] if stem(p['cam']) not in units]
for p in todo:
    x = wav(os.path.join(a.media, p['cam'])).astype(np.float32)
    hop = SR // 100
    n = len(x) // hop
    # +1 floor: noise-suppressed recordings contain digital silence; without a floor the "noise" percentile
    # drops to -180 dB and the threshold lets almost everything count as speech
    db = 20 * np.log10((np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1)) + 1) / 32768)
    noise, speech = np.percentile(db, 10), np.percentile(db, 90)
    sp = db > noise + 0.35 * (speech - noise)

    def runs(b):
        r, s = [], 0
        for i in range(1, len(b) + 1):
            if i == len(b) or b[i] != b[s]:
                r.append((s, i, bool(b[s]))); s = i
        return r
    for s, e, v in runs(sp):          # fill <80 ms gaps, drop <60 ms blips
        if not v and e - s < 8: sp[s:e] = True
    for s, e, v in runs(sp):
        if v and e - s < 6: sp[s:e] = False
    regs = [[s / 100, e / 100] for s, e, v in runs(sp) if v and (e - s) >= 12]
    merged = []
    for s, e in regs:                  # only silences >= min_silence split units
        if merged and s - merged[-1][1] < a.min_silence:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    units[stem(p['cam'])] = [{'s': s, 'e': e} for s, e in merged]

# ---------------------------------------------------------------- 3. transcribe each unit in isolation
GAP, PAD = 2.0, 0.12
def transcribe(p):
    k = stem(p['cam'])
    us = units[k]
    if us and 't' in us[0]:
        return
    x = wav(os.path.join(a.media, p['cam']))
    parts, spans, t = [], [], 0.0
    for u in us:
        s0, s1 = max(0, int((u['s'] - PAD) * SR)), min(len(x), int((u['e'] + PAD) * SR))
        seg = x[s0:s1]
        spans.append((t, t + len(seg) / SR, u['s'] - PAD))
        parts += [seg, np.zeros(int(GAP * SR), np.int16)]
        t += len(seg) / SR + GAP
    cw = os.path.join(a.work, 'wav', f'units_{k}.wav')
    with wave.open(cw, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(np.concatenate(parts).tobytes())
    tj = os.path.join(a.work, f'whisper_{k}.json')
    whisper(cw, tj)
    words = [json.loads(l) for l in open(tj, encoding='utf-8') if l.strip()]
    for u in us:
        u['t'], u['wt'] = '', []
    for w_ in words:
        st = w_['start'] / 1000          # word START times are reliable; end times bleed into gaps
        for u, (a0, b0, off) in zip(us, spans):
            if a0 - 0.4 <= st <= b0 + 0.3:
                u['wt'].append([w_['text'].strip(), round(st - a0 + off, 2)])
                break
    for u in us:
        u['t'] = ' '.join(w for w, _ in u['wt'])

with ThreadPoolExecutor(a.jobs) as ex:
    list(ex.map(transcribe, proj['pairs']))
json.dump(units, open(uj, 'w', encoding='utf-8'), ensure_ascii=False)
for p in proj['pairs']:
    k = stem(p['cam'])
    with open(os.path.join(a.work, f'ut_{k}.txt'), 'w', encoding='utf-8') as fh:
        prev = 0.0
        for i, u in enumerate(units[k]):
            fh.write(f"{i:3d} {u['s']:7.2f}-{u['e']:7.2f} gap{u['s'] - prev:5.1f} | {u['t']}\n")
            prev = u['e']
    print(f'units {k}: {len(units[k])}  -> ut_{k}.txt')
