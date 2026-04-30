# UMS Stats Crawler

UMS 관리자 페이지에서 전일 알림톡 통계를 고객사별로 수집해 Google Sheets에 적재하는 스크립트입니다.

## 동작 개요

- UMS 로그인
- 실시간 결과 통계 페이지 이동
- 메시지 유형을 `알림톡`으로 설정
- 고객사별로 `발송`과 `성공` 건수 조회
- Google Sheets `report` 시트에 `date + customer` 기준 업서트

## 필요 환경변수

- `ADMIN_EMAIL`: UMS 관리자 계정 이메일
- `ADMIN_PASSWORD`: UMS 관리자 계정 비밀번호
- `GCP_SERVICE_ACCOUNT_JSON`: Google Sheets 접근용 서비스 계정 JSON 문자열
- `TARGET_DATE`: 선택 사항. `YYYY-MM-DD` 형식의 수집 대상 일자

`TARGET_DATE`를 비워두면 `Asia/Seoul` 기준 전일자를 자동으로 사용합니다.

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python collect_stats.py
```

특정 날짜를 다시 수집하려면:

```bash
TARGET_DATE=2026-04-30 python collect_stats.py
```

## GitHub Actions 실행

자동 실행:

- 매일 오전 6시 KST

수동 실행:

- Actions에서 `UMS 통계 수집` 워크플로우 선택
- `Run workflow` 클릭
- 필요하면 `target_date`에 `YYYY-MM-DD` 입력

`target_date`를 비워두면 자동 실행과 동일하게 KST 전일 기준으로 동작합니다.

## 적재 방식

Google Sheets `report` 시트에 아래 순서로 저장합니다.

- `date`
- `customer`
- `total`
- `success`
- `rate`

같은 `date + customer` 행이 이미 있으면 update 하고, 없으면 append 합니다.

## 장애 확인 포인트

- 로그인 실패 시 `debug_login_fail.png` 생성
- 통계 화면 진입 후 디버그 스크린샷은 `debug_stats_page.png`로 남음
- GitHub Actions에서는 `debug_*.png`가 artifact로 업로드됨

## 주의사항

- UMS 화면 구조나 드롭다운 DOM이 바뀌면 선택자 보강이 필요할 수 있습니다.
- 시트 헤더는 `date`, `customer` 기준으로 관리하는 것을 권장합니다.

## 배포 전 체크리스트

- GitHub Actions secrets에 `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `GCP_SERVICE_ACCOUNT_JSON`가 모두 등록되어 있는지 확인
- Google Sheets의 `report` 시트가 존재하는지 확인
- 서비스 계정에 대상 스프레드시트 접근 권한이 있는지 확인
- UMS 계정으로 통계 페이지 접근 권한이 있는지 확인
- 수동 실행으로 `target_date` 한 번 테스트하고 결과가 기대값과 맞는지 확인
- 실패 시 Actions artifact의 `debug_*.png`를 확인
