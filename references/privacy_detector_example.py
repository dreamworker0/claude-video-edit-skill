"""Example per-frame detector for privacy_masks.py ("detector": "privacy_detector_example.py:report_masks").

Used on a scrolling monthly activity report (주간활동서비스 월별 활동계획 및 기록지) shown in a white modal.
Nothing on the page has a fixed y, so each frame we find anchors that are easy to detect by colour:
  * the beige "총 활동시간" header cell -> the 작성자 / 수급자 / 담당 제공인력 rows sit right above it
  * the red seal (직인) -> the organisation name sits left of it, the signature name below it
  * the service-provider column has a fixed x, so it is a vertical strip below the header table
Return a FIXED number of slots (None when absent) so each mask keeps its identity frame to frame.
Coordinates are 1920x1080 screen pixels. Measure them on a gridded screenshot of your own screen first.
"""
import numpy as np

H = 1080

def report_masks(f):
    R, G, B = (f[..., k].astype(np.int16) for k in range(3))
    out = [None] * 6
    white = ((R > 235) & (G > 235) & (B > 235))[:, 360:1510].mean()
    if white < 0.35:                                    # report not on screen
        return out
    beige = (R > 235) & (G > 215) & (G < 250) & (B > 150) & (B < 228) & (R - B > 20)
    rows = np.nonzero(beige[:, 745:960].mean(1) > 0.6)[0]
    strip_top = 60
    if len(rows) and rows.min() > 60:
        bt = int(rows.min())
        out[0] = [526, bt - 90, 737, bt + 2]           # 작성자 / 수급자(성명) values
        out[1] = [848, bt - 48, 1476, bt + 2]          # 담당 제공인력 value
        strip_top = bt + 205                           # daily table starts below the header table
    out[2] = [460, strip_top, 544, H]                  # 서비스 제공자 column
    red = (R > 150) & (R - G > 60) & (R - B > 60)
    ys, xs = np.nonzero(red[:, 1250:1530])
    if len(xs) > 300:
        x0, x1, y0, y1 = xs.min() + 1250, xs.max() + 1250, ys.min(), ys.max()
        cy = (y0 + y1) // 2
        out[3] = [x0 - 8, y0 - 8, x1 + 8, y1 + 8]      # seal
        out[4] = [1030, cy - 20, x0, cy + 20]          # organisation name line
        out[5] = [x0 - 70, y1 + 12, x1 - 20, y1 + 56]  # signature name
    return out
