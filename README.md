# claude-video-edit-skill

[Claude Code](https://claude.com/claude-code)용 영상 편집 스킬입니다. 프리미어 없이 FFmpeg로 편집합니다.

- **A. 프리미어 프로젝트 재현**: `.prproj`를 분석해 컷 목록과 효과(위치·크기, 자르기, 울트라 키 크로마키, 마스크
  모자이크, 화면 정지, 트랙 음소거, 속도)를 뽑고, 같은 영상을 FFmpeg로 다시 렌더링한 뒤 원본 내보내기와
  화질(PSNR)·소리 싱크를 비교합니다.
- **B. 원본 녹화로 처음부터 편집**: 화면 녹화 + 그린스크린 카메라 원본을 음성 인식으로 받아써서, 다시 말한 부분·끊긴
  문장·군소리·긴 침묵을 자르고, 카메라를 키잉해 얹고, 표지·핸드폰 녹화(B-roll)를 넣고, 화면 속 개인정보를
  모자이크하고, 결과를 다시 받아써서 확인합니다.

Claude에게 "이 프리미어 프로젝트 분석해서 다시 렌더링해줘", "원본만 가지고 편집해줘", "말 반복한 거 잘라줘"처럼
말하면 스킬이 쓰입니다.

## 설치

```bash
git clone https://github.com/dreamworker0/claude-video-edit-skill ~/.claude/skills/premiere-ffmpeg-render
```

Windows에서는 `C:\Users\<이름>\.claude\skills\premiere-ffmpeg-render`에 받으면 됩니다. 업데이트는 그 폴더에서 `git pull`.

필요한 것:
- FFmpeg (B는 whisper 필터가 들어간 빌드 — `ffmpeg -filters | grep whisper`로 확인. Windows: `winget install Gyan.FFmpeg`)
- Python 3 + `pip install numpy pillow`
- B의 음성 인식 모델: [whisper.cpp ggml 모델](https://huggingface.co/ggerganov/whisper.cpp)
  (예: `ggml-large-v3-turbo-q5_0.bin`, 약 550MB)을 `~/.cache/whisper/`에. 없으면 Claude가 받아도 되는지 묻습니다.

## 구성

| 파일 | 내용 |
|---|---|
| `SKILL.md` | Claude가 따르는 작업 순서 |
| `scripts/extract.py` | `.prproj` → 타임라인 JSON (재현 못 하는 효과는 경고) |
| `scripts/render.py` | 타임라인 → mp4 (구간별 렌더링, 내용 기준 캐시로 부분 수정 시 바뀐 구간만 다시) |
| `scripts/verify.py` | 원본과 프레임 수·PSNR·소리 싱크 비교 |
| `scripts/scratch_prep.py` | 원본 짝 맞추기·싱크·말 덩어리·음성 인식 |
| `scripts/scratch_edl.py` | 편집 결정 → 클립 목록 + 다시 받아쓰기 확인 |
| `scripts/scratch_timeline.py` | 표지·B-roll·카메라 배치·모자이크를 넣은 타임라인 |
| `scripts/privacy_review.py`, `privacy_masks.py` | 개인정보 확인용 장면 모음, 모자이크 영역 |
| `references/editing_from_raw.md` | 무엇을 자를지 판단 기준과 함정 |
| `references/editing_profile.md` | **편집 습관 프로필** — 자기 스타일로 바꿔 쓰세요 |
| `references/notes.md` | `.prproj` 포맷 해독 메모 |

## 한계

- 울트라 키는 근사치입니다(머리카락 가장자리가 조금 다를 수 있음). 전환 효과, 불투명도/페이드, 회전, 오디오
  볼륨·속도 조정, 텍스트/그래픽, Lumetri 등은 재현하지 않습니다(추출 시 경고).
- 그린스크린 카메라 + 화면 녹화로 찍은 설명 영상으로 시험했습니다.
- 음성 인식과 렌더링은 내 컴퓨터에서 처리되지만, 편집 판단을 위해 대본과 화면 캡처는 Claude와의 대화로 전달됩니다.

## 라이선스

MIT
