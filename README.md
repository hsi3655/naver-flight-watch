# 오사카 주말 항공권 감시 (GitHub Actions · PC 꺼져도 동작)

네이버 항공권에서 **서울(인천·김포) → 오사카(간사이) 2박 3일(금~일, 토~월)** 왕복을 조회 가능한 날짜 전체로 조회해,
**가는 편 06~12시 / 오는 편 12~18시 출발, 직항, 피치항공 제외** 조건에서
**네이버페이 / 우리카드 결제 기준 최저가 TOP 10**을 **매일 오전 8시·오후 8시(한국 시간)** 메일로 보냅니다.

GitHub 서버에서 돌기 때문에 **PC를 꺼 두어도 됩니다.** 비용은 없습니다(공개 저장소 기준).

---

## 1. 저장소 만들기

1. GitHub 로그인 → 오른쪽 위 **+** → **New repository**
2. Repository name: `naver-flight-watch` (아무 이름이나 가능)
3. **Public** 선택 → **Create repository**
   - 비공개(Private)는 무료 실행 시간이 월 2,000분이라, 1회 약 1시간씩 하루 2번이면 부족합니다.
   - 공개여도 **메일 주소·앱 비밀번호는 Secrets(3단계)에 넣으므로 남에게 보이지 않습니다.**
     저장소에 보이는 것은 프로그램과 가격 기록(`data/history.csv`)뿐입니다.

## 2. 파일 올리기

1. 받은 zip 압축을 풉니다. 폴더 안에 `.github` 폴더, `naver_api_watch.py`, `config.json`, `requirements.txt`, `README.md`, `data` 폴더가 있어야 합니다.
2. 새 저장소 화면에서 **uploading an existing file** 링크(또는 **Add file → Upload files**) 클릭
3. 압축 푼 폴더 **안의 내용 전부**(`.github` 폴더 포함)를 끌어다 놓기 → 아래 **Commit changes**
4. 저장소 첫 화면에 `.github` 폴더가 보이면 성공입니다.
   - `.github` 폴더가 안 올라갔다면: **Add file → Create new file** → 파일 이름 칸에 `.github/workflows/watch.yml` 입력 →
     zip 안의 같은 파일 내용을 붙여 넣고 Commit.

## 3. 메일 정보 등록 (Secrets)

저장소 **Settings → Secrets and variables → Actions → New repository secret** 으로 3개 등록:

| Name | Secret |
|---|---|
| `MAIL_USER` | 보내는 Gmail 주소 |
| `MAIL_APP_PASSWORD` | Gmail 앱 비밀번호 16자리 (PC 버전에서 쓰던 것 그대로) |
| `MAIL_TO` | 받을 주소 (여러 개면 쉼표로 구분) |

## 4. 시험 실행 (순서대로)

저장소 **Actions** 탭 → (처음이면 "I understand my workflows…" 활성화 버튼) → 왼쪽 **오사카 항공권 감시** → 오른쪽 **Run workflow**

| 순서 | mode | 확인할 것 |
|---|---|---|
| ① | `test-mail` | "메일 설정 확인" 메일 도착 |
| ② | `probe` | 실행 기록(초록 체크 → `watch` → `실행` 단계)에 `[네이버페이] 최저 ○○원`, `[우리카드] 최저 ○○원` 이 나오면 정상 |
| ③ | `limit` (2) | 일정 2개만 조회해서 결과 메일 도착 |

- ②에서 오류가 나거나 결과가 0개면: 실행 화면 아래 **Artifacts** 의 `result-번호` 를 내려받아 Claude에게 보여 주세요
  (`probe/fare_types.json`, `probe/response_sample.json` 으로 원인을 바로 찾을 수 있습니다).

## 5. 이후

- 매일 **08:07, 20:07(한국 시간)** 에 자동 실행 → 끝나면 메일. GitHub 사정으로 **5~30분 늦게** 시작될 수 있습니다.
- 1회 실행 시간은 약 1시간입니다(검색 약 190건).
- 실행 오류가 나면 **"[항공권 감시] 실행 오류"** 메일이 옵니다. GitHub도 실패 알림 메일을 따로 보냅니다.
- 가격 기록은 저장소의 `data/history.csv`(최근 120일)에 쌓이고, "지난번 대비" 비교에 쓰입니다.
- 자동 실행을 멈추려면: Actions → 오사카 항공권 감시 → 오른쪽 `…` → **Disable workflow**

## 6. 설정 바꾸기 (`config.json` — 저장소에서 연필 아이콘으로 수정)

| 항목 | 기본값 | 의미 |
|---|---|---|
| `origins` | `["ICN","GMP"]` | 출발 공항. 김포를 빼면 실행 시간이 절반 |
| `outbound_depart_between` | `["06:00","12:00"]` | 가는 편 출발 시각 범위 (3시간 단위 칸: 06·09·12·15·18·21시 경계로 맞추면 정확) |
| `return_depart_between` | `["12:00","18:00"]` | 오는 편 출발 시각 범위 |
| `payment_groups` | 네이버페이 / 우리카드 | 결제수단 이름(네이버 운임 이름에 들어간 글자) |
| `exclude_airlines` | 피치 | 한 구간이라도 포함되면 제외 |
| `nonstop_only` | `true` | 직항만 |
| `top_n` | 10 | 결제수단별 표시 개수 |
| `mail.only_when_changed` | `false` | `true`면 TOP 목록이 바뀔 때만 메일 |

알림 시각을 바꾸려면 `.github/workflows/watch.yml` 의 `cron` 두 줄을 고칩니다(UTC 기준 = 한국 시간 − 9시간).

## 7. 알아 두실 점

- **결제가 계산**: 결제수단별 가격 = 그 결제수단 할인 운임과 일반(모든 결제수단) 운임 중 낮은 값(성인 1인, 세금 포함).
  할인 운임은 카드 실적 등 조건이 붙을 수 있습니다. 메일 표의 "할인 적용" 표시와 "모든 결제수단" 열로 확인하세요.
- **비공식 방식**: 네이버 항공권 사이트가 내부적으로 쓰는 검색 주소를 직접 부릅니다. 네이버가 바꾸면 조회가 실패할 수 있고,
  그 경우 오류 메일이 옵니다. 하루 2번 정도의 무리 없는 빈도를 유지하세요.
- **GitHub 규칙**: 공개 저장소는 60일간 활동이 없으면 예약 실행이 멈춥니다. 이 프로그램은 실행마다 가격 기록을 저장소에
  저장하지만 이것이 '활동'으로 인정되는지는 확실하지 않습니다. GitHub가 "곧 비활성화" 메일을 보내면
  Actions 화면에서 다시 켜거나 `config.json` 을 한 번 수정·저장하세요.
