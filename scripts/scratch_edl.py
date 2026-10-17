"""Edit-from-raw, step 2: your keep/trim decisions -> clip list (edl.json) + rough-cut audio check.

Usage:
  python scratch_edl.py <work_dir> <decisions.json> [--check --model M.bin] [--window START:LEN ...]

decisions.json
{
  "files": {                                   # camera file stem -> decisions (files play in project.json order)
    "2026-08-16 08-14-27": {
      "keep": [0, 1, 5, "9-12", 16],           # unit indices from ut_<stem>.txt ("a-b" = inclusive range)
      "trim": {"5": ["end", 31.45, 31.75],     # cut inside a unit at the quietest 20 ms of [from, to] (seconds)
               "9": [["start", 45.6, 46.0], ["cut", 50.2, 50.6, 54.0, 54.4]]}   # "cut": drop the middle of a unit
                                               # (from the quietest point of a to the quietest of b); list = several ops
    }
  },
  "pre": 0.10, "post": 0.22, "merge_gap": 0.55 # padding around speech; neighbours closer than merge_gap stay one clip
}
Instead of "files", "sequence": [{"cam": "<stem>", "keep": [...], "trim": {...}}, ...] plays blocks in the
given order; the same file may appear in several blocks (e.g. a summary recorded last, used as the opening hook).
--check   transcribes the whole rough cut and prints it with mm:ss and clip numbers - read it like a viewer would
--window  transcribes only START:LEN seconds of the rough cut (fast re-check after fixing a spot)
"""
import argparse, bisect, json, os, subprocess, wave
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('work'); ap.add_argument('decisions')
ap.add_argument('--check', action='store_true'); ap.add_argument('--model')
ap.add_argument('--window', action='append', default=[])
ap.add_argument('--lang', default='ko')
a = ap.parse_args()
SR = 16000
proj = json.load(open(os.path.join(a.work, 'project.json'), encoding='utf-8'))
units = json.load(open(os.path.join(a.work, 'units.json'), encoding='utf-8'))
dec = json.load(open(a.decisions, encoding='utf-8'))
PRE, POST, MERGE = dec.get('pre', 0.10), dec.get('post', 0.22), dec.get('merge_gap', 0.55)
stem = lambda f: os.path.splitext(os.path.basename(f))[0]

def expand(keep):
    out = set()
    for k in keep:
        if isinstance(k, str) and '-' in k:
            lo, hi = (int(v) for v in k.split('-')); out |= set(range(lo, hi + 1))
        else:
            out.add(int(k))
    return sorted(out)

pcm = {}
def load(k):
    if k not in pcm:
        with wave.open(os.path.join(a.work, 'wav', k + '.wav')) as w:
            pcm[k] = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    return pcm[k]

pair_of = {stem(p['cam']): p for p in proj['pairs']}
# blocks in playback order: decisions["sequence"] (any order, a file may appear more than once - e.g. a summary
# recorded at the end used as the opening hook), else every file in "files" in recording order
if 'sequence' in dec:
    blocks = [(b['cam'], b) for b in dec['sequence']]
else:
    blocks = [(stem(p['cam']), dec['files'][stem(p['cam'])]) for p in proj['pairs'] if stem(p['cam']) in dec['files']]

clips = []
for k, d in blocks:
    p = pair_of[k]
    us = units[k]
    keep = expand(d.get('keep', []))
    trims = {int(i): v for i, v in d.get('trim', {}).items()}
    x = load(k).astype(np.float32)
    def quietest(lo, hi):
        hop, i0, i1 = 160, int(lo * SR), int(hi * SR)
        e = [np.sqrt((x[i:i + 320] ** 2).mean()) for i in range(i0, max(i0 + 1, i1 - 320), hop)]
        return lo + (int(np.argmin(e)) * hop + 160) / SR
    spans = []
    for u in keep:
        s, e = us[u]['s'] - PRE, us[u]['e'] + POST
        holes = []
        ops = trims.get(u, [])
        if ops and isinstance(ops[0], str):
            ops = [ops]                                  # one op or a list of ops per unit
        for op in ops:
            if op[0] == 'end':
                e = quietest(op[1], op[2])
            elif op[0] == 'start':
                s = quietest(op[1], op[2])
            elif op[0] == 'cut':                         # remove the middle: from quietest(a) to quietest(b)
                holes.append((quietest(op[1], op[2]), quietest(op[3], op[4])))
        if u > 0 and (u - 1) not in keep: s = max(s, us[u - 1]['e'] + 0.03)      # never reach into dropped speech
        if u + 1 < len(us) and (u + 1) not in keep: e = min(e, us[u + 1]['s'] - 0.03)
        edges = [s] + [v for h in sorted(holes) for v in h] + [e]
        for j in range(0, len(edges), 2):               # pieces between holes; a piece after a hole never merges back
            spans.append([u, edges[j], edges[j + 1], j > 0])
    merged = []
    for u, s, e, after_cut in spans:
        if merged and not after_cut and merged[-1][0] == u - 1 and s - merged[-1][2] <= MERGE:
            merged[-1][0], merged[-1][2] = u, e
        else:
            merged.append([u, s, e])
    for u, s, e in merged:
        clips.append({'cam': p['cam'], 'scr': p['scr'], 'lag': p['lag'], 's': round(max(0.0, s), 3), 'e': round(e, 3)})

json.dump(clips, open(os.path.join(a.work, 'edl.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
tot = sum(c['e'] - c['s'] for c in clips)
print(f'{len(clips)} clips, {int(tot // 60)}:{tot % 60:04.1f}  -> edl.json')

# rough-cut audio (camera voice only) for reading back
parts, marks, t = [], [], 0.0
for i, c in enumerate(clips):
    seg = load(stem(c['cam']))[int(c['s'] * SR):int(c['e'] * SR)]
    marks.append(t); parts.append(seg); t += len(seg) / SR
rough = os.path.join(a.work, 'wav', 'rough.wav')
with wave.open(rough, 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(np.concatenate(parts).tobytes())

def transcribe(start, length, tag):
    if not a.model:
        raise SystemExit('--model is required for --check / --window')
    esc = lambda p: "'" + os.path.abspath(p).replace('\\', '/').replace(':', '\\:') + "'"   # quoted, ':' escaped
    out = os.path.join(a.work, f'check_{tag}.json')
    cmd = ['ffmpeg', '-v', 'error', '-y']
    if length:
        cmd += ['-ss', str(start), '-t', str(length)]
    cmd += ['-i', rough, '-af', f'whisper=model={esc(a.model)}:language={a.lang}:queue=10:destination={esc(out)}:format=json',
            '-f', 'null', '-']
    subprocess.run(cmd, check=True)
    for l in open(out, encoding='utf-8'):
        if l.strip():
            d = json.loads(l); s = start + d['start'] / 1000
            ci = bisect.bisect_right(marks, s) - 1
            print(f'{int(s // 60):02d}:{s % 60:04.1f} [c{ci}] {d["text"]}')

if a.check:
    transcribe(0, 0, 'all')
for w_ in a.window:
    st, ln = (float(v) for v in w_.split(':'))
    print(f'== window {st}s +{ln}s'); transcribe(st, ln, f'{int(st)}')
