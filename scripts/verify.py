"""Compare reproduced render vs. original export: frame count, per-second video PSNR, audio sync/similarity."""
import subprocess, sys, re, wave, os
import numpy as np

MINE, ORIG, WORK = sys.argv[1:4]
probe = lambda f: subprocess.run(['ffprobe', '-v', 'error', '-count_packets', '-select_streams', 'v:0', '-show_entries',
                                  'stream=nb_read_packets:format=duration', '-of', 'compact', f],
                                 capture_output=True, text=True).stdout.strip()
print('mine :', probe(MINE))
print('orig :', probe(ORIG))

# video: PSNR on every 15th frame (2 per second), per-frame log
log = os.path.join(WORK, 'psnr.log').replace('\\', '/').replace(':', '\\:')
subprocess.run(['ffmpeg', '-v', 'error', '-i', MINE, '-i', ORIG, '-lavfi',
                f"[0:v]select='not(mod(n,15))',setpts=N/TB[a];[1:v]select='not(mod(n,15))',setpts=N/TB[b];"
                f"[a][b]psnr=stats_file='{log}'", '-f', 'null', '-'], check=True)
vals = []
for line in open(os.path.join(WORK, 'psnr.log')):
    m = re.search(r'n:(\d+).*psnr_avg:([\d.inf]+)', line)
    if m:
        vals.append(float(m.group(2)) if m.group(2) != 'inf' else 99.0)
v = np.array(vals)
t = np.arange(len(v)) * 15 * 1001 / 30000
print(f'video PSNR: samples={len(v)} mean={v.mean():.2f} median={np.median(v):.2f} '
      f'>=40dB {np.mean(v >= 40) * 100:.1f}%  >=35dB {np.mean(v >= 35) * 100:.1f}%  <30dB {np.sum(v < 30)}')
bad = t[v < 30]
if len(bad):
    print('  low-PSNR times (s):', ', '.join(f'{x:.1f}' for x in bad[:40]))

# audio: decode both to mono 8k, windowed correlation + lag
def mono(f):
    out = os.path.join(WORK, os.path.basename(f) + '.8k.wav')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', f, '-vn', '-ac', '1', '-ar', '8000', '-c:a', 'pcm_s16le', out], check=True)
    with wave.open(out) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
a, b = mono(MINE), mono(ORIG)
n = min(len(a), len(b))
win = 8000 * 5
cors, lags = [], []
for s in range(0, n - win, win):
    x, y = a[s:s + win], b[s:s + win]
    if x.std() < 50 or y.std() < 50:
        continue
    c = np.fft.irfft(np.fft.rfft(x, 2 * win) * np.conj(np.fft.rfft(y, 2 * win)))
    c = np.concatenate([c[-400:], c[:401]])        # lags -50ms..+50ms
    k = int(np.argmax(c)) - 400
    lags.append(k / 8)                              # ms
    cors.append(np.corrcoef(x, y)[0, 1])
cors, lags = np.array(cors), np.array(lags)
print(f'audio: windows={len(cors)} corr mean={cors.mean():.3f} min={cors.min():.3f}  '
      f'lag ms median={np.median(lags):.1f} max|lag|={np.abs(lags).max():.1f}')
print(f'audio length: mine {len(a) / 8000:.3f}s orig {len(b) / 8000:.3f}s')
