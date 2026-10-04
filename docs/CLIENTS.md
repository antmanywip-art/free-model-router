# 클라이언트 연결 계약

공통 URL은 배포 후 `https://router.134.185.103.138.sslip.io/v1`입니다. 아직 배포되지 않은 예정 주소입니다. `/chat/completions`에 Bearer 키로 요청합니다. 앱마다 서로 다른 라우터 키를 사용하며 제공자 키를 앱에 전달하지 않습니다.

## MAGI

수정된 MAGI에서 다음 환경변수를 주입합니다.

```text
MAGI_ROUTER_ENABLED=1
PERSONAL_ROUTER_BASE_URL=https://router.134.185.103.138.sslip.io/v1
PERSONAL_ROUTER_API_KEY=<별도로 안전하게 주입>
```

기본 모델 별칭은 `magi-a`, `magi-b`이며 `MAGI_ROUTER_MODEL_A/B`로 지정 경로를 선택할 수 있습니다. `magi.config.json`의 personal_router.allowed_hosts에 승인된 호스트를 둡니다. 환경변수만으로 임의 HTTPS 호스트에 키를 보낼 수 없습니다. 외부 HTTP와 리다이렉트는 차단합니다.

개인 라우터 모드에서는 직접 B.AI/OpenRouter 어댑터를 생성하지 않습니다. 라우터가 재시도·대체를 담당하므로 MAGI 자체 재시도는 0으로 제한합니다. 기존 plan/run 해시 계약은 유지하며 실제 라우터 주소가 승인 해시에 포함됩니다. 기존 개인정보 분류 정책은 변경하지 않았고 새로운 개인정보 차단 기능을 추가하지 않았습니다.

응답의 `router.model`, `router.family`를 작업 기록에 반영합니다. 두 별칭이 같은 계열로 귀결되면 독립 계열 두 곳의 합의로 취급하지 않습니다. 같은 계열이라도 제공 업체 이름이 다르다는 이유로 독립성이 생기지 않습니다. 모델 계열 표기는 검증된 라우터 설정에 의존하며, 숨겨진 제공자 내부 리매핑까지 증명하지는 않습니다. 기본 2명 구성에서는 3명 이상이 필요한 council peer round를 수행하지 않습니다.

## 나라장터

```text
PERSONAL_ROUTER_ENABLED=1
PERSONAL_ROUTER_BASE_URL=https://router.134.185.103.138.sslip.io/v1
PERSONAL_ROUTER_API_KEY=<나라장터 전용 키를 별도로 주입>
PERSONAL_ROUTER_MODEL=nara-text
PERSONAL_ROUTER_TIMEOUT_SECONDS=105
PERSONAL_ROUTER_SELECTION_BATCH_SIZE=8
PERSONAL_ROUTER_SUMMARY_BATCH_SIZE=1
```

이 모드는 ProcurementAiRouter의 선별·요약 전체에 적용됩니다. 실패하거나 키가 없으면 Gemini/B.AI로 직접 우회하지 않습니다. 기존 운영 방식은 opt-in을 켜기 전까지 유지됩니다.

선별 응답은 `matches` 배열, 요약은 `reports` 배열이어야 합니다. 입력의 공고 `key`를 정확히 반환해야 하며 잘못된 key·중복·형식 오류를 실패 처리합니다. 누락 공고는 요약 미완료로 남깁니다. 순서 보정이나 다른 공고와의 추정 매칭을 하지 않습니다.

첨부는 추출된 텍스트만 전송합니다. Gemini native의 PDF inline_data 기능은 이 경로에 포함되지 않습니다. OCR/스캔 PDF·HWP 추출 검증은 별도 과제입니다. 기존 추출 분량 제한과 미확인 조건 표시는 유지합니다. 운영 수집기·cron·SMTP를 클라우드 설정 검증용으로 실행하지 마세요.

## 로컬 통합 시험

`PERSONAL_ROUTER_ALLOW_LOOPBACK=1`은 합성 테스트에서만 사용합니다. 127.0.0.1/localhost/::1에 한해 HTTP를 허용합니다. 운영 예시에 이 변수를 넣지 않습니다. `scripts/integration_smoke.py`가 임시 환경과 가짜 키를 자동으로 구성하고 모든 서버를 종료합니다.
