# .prproj 포맷 메모와 함정 모음

## 파일 구조

- `.prproj` = gzip(XML). 최상위 `<PremiereData>` 아래에 모든 객체가 평평하게 나열되고,
  `ObjectID`/`ObjectRef`(정수) 또는 `ObjectUID`/`ObjectURef`(UUID)로 서로 참조한다. 탐색할 때는
  매 단계 참조를 풀어야 한다 (`deref`).
- 시간 단위: tick, **초당 254016000000**. 시퀀스 프레임 길이는 `VideoTrackGroup/TrackGroup/FrameRate`
  (29.97fps → 8475650266). 정확한 1001/30000초(8475667200)와 조금 달라서 tick → 프레임은 반올림한다.
- 탐색 경로:
  `Sequence/TrackGroups/TrackGroup/Second` → `VideoTrackGroup | AudioTrackGroup`
  → `TrackGroup/Tracks/Track` → `VideoClipTrack | AudioClipTrack`
  → `ClipTrack/ClipItems/TrackItems/TrackItem` → `VideoClipTrackItem`
  → `ClipTrackItem/TrackItem/{Start,End}` (타임라인 위치)
  → `ClipTrackItem/SubClip` → `Clip` → `Clip/{InPoint,OutPoint}` (원본 구간)
  → `Clip/Source` → `MediaSource/Media` → `FilePath`
- 시퀀스 크기: `VideoTrackGroup/FrameRect` = `0,0,1920,1080`.
- 효과: `ClipTrackItem/ComponentOwner/Components` → `VideoComponentChain` 안의 `Component`들.
  `MatchName`으로 종류 구분 (`AE.ADBE Motion`, `AE.ADBE AECrop`, `AE.ADBE Ultra Key`, `AE.ADBE Mosaic`).
  파라미터 `Name`은 **UI 언어로 저장**되므로(한국어 프로젝트면 "위치", "비율 조정") 반드시
  `ParameterID`로 읽는다. 값은 `CurrentValue` 또는 `StartKeyframe`의 두 번째 필드
  (`-91445760000000000,<값>,0,0,...`). `Keyframes` 요소가 있으면 애니메이션된 값이다.
- 기본값 그대로인 클립(모션/불투명도 손대지 않음)은 컴포넌트 체인이 비어 있다.

## 효과별 파라미터 (ParameterID)

- Motion: 1 위치 `x:y`(시퀀스 기준 0~1), 2 크기(%), 3 가로 크기, 4 균일 비율(bool), 5 회전, 6 기준점 `x:y`(클립 기준 0~1)
- Crop: 1 왼쪽, 2 위, 3 오른쪽, 4 아래 (%), 6 가장자리 페더
- Ultra Key: 3 키 색상 = 64비트 정수. 16비트씩 ARGB이고 8비트 값은 각 16비트의 상위 바이트.
  예) `18374698575994045696` = `0xff00_0b00_6500_4500` → RGB(11,101,69). 5 투명도, 8 허용치, 9 페데스탈.
- 오디오 트랙 `AudioTrack/ComponentOwner/Components` → `AudioFader`의 파라미터 순서가 [볼륨, 음소거].
  음소거 버튼이 켜진 트랙은 렌더링에서 빼야 한다 (화면녹화 소리 + 카메라 소리가 같이 들어간 프로젝트에서
  한쪽만 켜 둔 경우가 흔하다).

## 마스크 (모자이크 등 효과 마스크)

- 효과의 `SubComponents` 아래 `AE.ADBE AEMask` 하나가 마스크 하나. `ParameterID` 6 = 마스크 패스
  (`ArbVideoComponentParam`), 7 페더, 8 불투명도, 9 확장, 10 반전.
- 패스 바이너리(base64):
  - `2cin`: 헤더 4바이트 + uint32 버전 + uint64 꼭짓점 수(오프셋 12), 오프셋 20부터 꼭짓점마다
    float 6개(점 x,y / 들어오는 접선 / 나가는 접선, 클립 기준 0~1) + 8바이트 플래그 = 32바이트 간격.
  - `kcin`: 구형/기본 사각형. uint32 개수(오프셋 8), 오프셋 16부터 28바이트 간격.
  - 접선이 점과 같으면 직선 다각형이다 (지금까지 본 마스크는 전부 그랬다).
- 마스크 추적/키프레임: `Keyframes` 텍스트가 `tick,base64;tick,base64;...`. **tick은 원본 미디어 시간**이다
  (클립 InPoint와 같은 기준). 프레임마다 키가 있는 경우(마스크 추적)가 많고, 마지막 키 이후는 유지.
  이 경우 `StartKeyframeValue`가 비어 있거나 기본 사각형일 수 있으니 키프레임을 우선한다.

## 모자이크가 프리미어와 같아지는 조건

- 처음엔 블록을 화면 전체(1920/가로블록 × 1080/세로블록)로 나눴더니 원본과 색이 달랐다.
- 프리미어는 **효과에 붙은 모든 마스크를 감싸는 영역(화면 밖은 잘라냄)**을 레이어로 보고 블록을 나눈다.
  가로 블록 1, 세로 10이면 그 영역 폭 전체가 한 칸이 된다. 이렇게 바꾸자 마스크 안 평균 오차가
  15 → 4로 줄었다. 마스크별 영역이나 화면 전체 기준은 둘 다 틀렸다.
- 마스크 키프레임이 매 프레임 바뀌기 때문에 FFmpeg 필터 대신 numpy로 프레임마다 처리한다
  (yuv444p 평면으로 받아 행렬 변환 없이 처리 → 인코더 stdin으로 전달).

## 크로마키

- FFmpeg `chromakey`(UV 거리)는 그린스크린이 어둡고 채도가 낮으면 회색 옷까지 지워버린다.
  `colorkey`·`hsvkey`도 그린스크린 밝기 편차 때문에 실패했다.
- 잘 된 방식: "화면색 우세도" d = G − (R+B)/2. 배경은 d≈30~90, 사람(피부·회색 옷·검은 머리)은 d≤0.
  alpha = clip((42 − d)/24, 0, 1), 번짐 억제 G' = min(G, max(R,B)+6). `geq`로 구현하되 **축소 후에**
  적용하면(예: 538×340) 속도가 충분하다.
- 클립별 울트라 키 '투명도' 값 차이(35~55)는 반영하지 않는다. 결과 차이는 머리카락 가장자리 정도.

## FFmpeg 함정

- 입력 두 개를 `-ss`로 각각 시킹하면 첫 프레임 PTS가 미세하게 달라서 overlay가 전경 없이 첫 프레임을
  내보낸다 → 모든 입력에 `setpts=N/(30000/1001)/TB`를 걸어 프레임 번호 기준으로 맞춘다.
- `-ss`는 목표 시각에서 0.25프레임 앞을 준다. 정확히 맞추면 반올림 때문에 다음 프레임이 잡힐 수 있다.
- FFmpeg 7 이후 `-filter_complex_script`가 없어졌다 → `-/filter_complex 파일`.
- concat demuxer는 목록 파일 기준 상대경로로 해석한다 → 목록에는 절대경로를 쓴다.
- NVENC는 드라이버 버전이 낮으면 열리지 않는다 (`Required: 13.1 Found: 13.0`). libx264로 돌린다.
- Windows Git Bash에서 `drawtext`는 fontconfig 오류로 세그폴트가 날 수 있다 → 라벨은 Pillow로 그린다.
- 콘솔이 cp949라 한글 출력이 깨지면 `PYTHONIOENCODING=utf-8`.

## 검증 기준 (이 스킬을 만든 프로젝트의 결과)

24분 23초, 클립 211+109개, 크로마키·모자이크 17곳·화면 정지 2곳 프로젝트에서
평균 PSNR 48.9dB(99.5%가 40dB 이상, 최저 38.2dB = 모자이크 구간), 소리 상관 0.999, 싱크 0ms,
프레임 수는 원본보다 1프레임 많음(43,866 vs 43,865 — 프리미어 내보내기가 마지막 프레임을 뺀 것으로 보임).
