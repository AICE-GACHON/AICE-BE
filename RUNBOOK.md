# RUNBOOK — 프로덕션 운영

**이 문서는 항상 최신이어야 합니다.** "왜 이렇게 정했는지"는 여기 없습니다 —
[docs/배포_계획.md](docs/배포_계획.md)(§0~8)를 보세요. 여기는 **지금 무엇이 떠 있고,
무엇을 해야 하고, 뭘 밟으면 안 되는지**만 담습니다.

> ⚠️ 이 문서는 2026-08-19에 `docs/배포_계획.md` §9(진행 상황 로그, 2026-08-11 기준
> 마지막 갱신)에서 "지금도 유효해야 하는 것"만 추려 처음 만들었습니다. §9 자체는
> 그 시점까지의 이력 기록으로 남겨뒀고, 더 이상 갱신하지 않습니다 — **이 시점 이후의
> 변경은 이 RUNBOOK에 직접 반영하세요.**

---

## 0. 지금 상태 (마지막 확인: 2026-08-11)

- **떠 있음**: https://paperaireview.com (2026-08-08부터)
- **아직 사용자를 초대하지 않았습니다.** "떠 있는 상태"이지 "베타 운영 중"이 아닙니다.
- 서버가 보고 있는 브랜치: `main @ 022964c` (PR #12 머지 시점, 2026-08-11 확인)

### ⚠️ 이 문서를 열었다면 먼저 확인할 것

**`develop`은 이 배포 이후에도 계속 나갔습니다** (온보딩 개편·약관 동의·공유 링크·
분석 진행상황·본문 diff 등, [CHANGELOG.md](CHANGELOG.md) 2026-08-16~17 참고). 서버가
그 커밋들을 반영했는지 이 문서만으로는 알 수 없습니다 — **`ssh` 후 `git log -1`로
서버가 보고 있는 커밋을 확인하고, 필요하면 아래 "배포" 절차로 갱신하세요.**

### 🔴 열려 있는 위험 — 다음 배포/운영 전에 반드시 확인

| 위험 | 설명 |
|---|---|
| **복구 리허설 미완료** | 자동 백업(RDS)은 돌지만 스냅샷에서 실제 복원을 해본 적이 없습니다. **이게 닫히기 전에는 실사용자를 받으면 안 됩니다** — 백업은 복구를 해봐야 백업입니다. |
| **새 `Dockerfile`(락파일 적용판)로 빌드해본 적 없음** | `requirements.lock.txt` 도입(2026-08-11) 이후 실제 배포 빌드가 한 번도 없었습니다. 다음 배포가 첫 빌드이고, torch부터 캐시가 무효화돼 4GB 박스에서 오래 걸립니다 — 한산한 시간에, 로그를 보면서 하세요. |
| **SES는 샌드박스** | 프로덕션 액세스가 거절됐습니다. 검증 안 된 주소로 재설정 메일을 요청하면 사용자에게는 "보냈습니다"(200)가 뜨지만 실제로는 안 갑니다(로그에만 554). 베타 테스터를 늘릴 때마다 그 주소를 SES에 검증 등록해야 합니다. |

---

## 1. 배포 절차

🟡 **한산한 시간에 하세요.** 분석이 `BackgroundTasks`라 재시작이 진행 중인 분석을
죽입니다.

```bash
ssh -i ~/paperai-key.pem ubuntu@paperaireview.com
cd ~/AICE-BE && git pull
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps      # api가 healthy 인지
docker compose -f docker-compose.prod.yml logs -f api
```

`ps`에서 `healthy`가 뜨기 전까지는 배포가 끝난 게 아닙니다 — 설정이 틀리면
컨테이너가 기동을 거부하고 멈춥니다(의도된 동작). `WARMUP_ON_STARTUP=1`이라
SPECTER2 로드에 수십 초 걸립니다.

⚠️ **DB 마이그레이션이 낀 배포는 코드만 되돌린다고 롤백되지 않습니다.**
`alembic upgrade`가 이미 스키마를 바꿨다면 `alembic downgrade`가 따로 필요하고,
데이터를 지우는 마이그레이션이면 그것도 안 됩니다. 그런 배포 전에는 **RDS
스냅샷을 수동으로 하나 떠두세요.**

### 프론트만 바뀐 경우

재빌드 없이 `dist/`만 올리면 됩니다.

```bash
rsync -av --delete -e "ssh -i ~/paperai-key.pem" \
  dist/ ubuntu@paperaireview.com:~/AICE-BE/frontend-dist/
```

프론트 빌드 전 체크리스트(빌드 가드가 대부분 막지만, 배포된 번들에서 최종 확인):
`grep -c mock dist/assets/*.js`가 0인지, `VITE_API_BASE_URL`이 실제 도메인으로
박혔는지.

## 2. 롤백 절차

```bash
git log --oneline -5          # 돌아갈 커밋 확인
git checkout <이전 커밋>
docker compose -f docker-compose.prod.yml up -d --build
```

🔴 **`docker compose down -v`는 절대 쓰지 마세요.** `caddy_data` 볼륨의 TLS
인증서가 함께 날아가고, Let's Encrypt는 발급 상한이 있습니다.

## 3. 잊으면 안 되는 함정

실제로 밟았던 것들입니다. 전부 조용히 실패하는 종류입니다.

- 🔴 **Cloudflare는 회색 구름(DNS only) 유지.** 주황 구름을 켜면 Caddy가 인증서를
  못 받고, `X-Forwarded-For` 홉이 늘어 **전 사용자가 rate limit 바구니 하나를
  공유**합니다(로그인 30/hour → 서비스 전체 30회).
- 🔴 **프론트를 `VITE_API_BASE_URL` 없이 빌드하면 화면 전체가 mock으로 동작**합니다
  — 가입·로그인·분석이 전부 가짜로 "성공"합니다.
- 🔴 **`docker compose down -v`를 쓰지 마세요.** (위 롤백 절차 참고.)
- 🔴 **DB 덤프는 코퍼스만이 아닙니다.** 전체 DB 덤프라 `users`·`submissions`(PDF
  원본 포함)가 함께 들어 있습니다. 그대로 복원하면 초대 게이트를 통과한 적 없는
  계정이 프로덕션에 생깁니다 — 반드시 `--exclude-table-data`로 새로 뜨고, 복원 후
  `users`·`submissions` 건수가 0인지 확인하세요.
- 🔴 **보내는 주소 키는 `SMTP_FROM`입니다. `MAIL_FROM`이 아닙니다.** 설정이
  `extra="ignore"`라 이름을 틀리면 아무 말 없이 무시되고, 재설정 메일이 한 통도
  안 나갑니다.
- 🔴 **병렬 HNSW 빌드는 `maintenance_work_mem`을 `/dev/shm`에서 가져갑니다.** 값을
  넉넉히 준 것이 오히려 복원을 죽입니다(`could not resize shared memory segment`).
  `max_parallel_maintenance_workers = 0`으로 피합니다.
- 🟡 **`ALLOWED_HOSTS`에서 `localhost`를 빼지 마세요.** 헬스체크가 400을 맞고
  컨테이너가 영원히 unhealthy가 됩니다.
- 🟡 **`.dockerignore`는 줄 끝 주석 미지원, `*`는 `/`를 안 넘음.** 빌드 로그의
  `transferring context:` 크기로 코퍼스 덤프가 안 딸려가는지 확인하세요.
- 🟡 **Caddyfile에 선언한 도메인은 DNS가 먼저 있어야 합니다.** 순서는 DNS 먼저,
  Caddy 나중 — 아니면 인증서 검증이 NXDOMAIN으로 반복 실패하고, 그것도 Let's
  Encrypt 한도에 잡힙니다. 백오프가 걸렸으면 `docker compose restart caddy`로
  즉시 재시도.
- 🟡 **`/docs`가 200이어도 놀라지 마세요.** Caddy가 `/api/*`만 백엔드로 보내고
  나머지는 SPA 폴백입니다. FastAPI 문서가 닫혔는지는 `/api/docs`로 확인하세요.
- 🟡 **덤프를 뜰 수 있는 PC가 한정됩니다.** 코퍼스는 git에 없어서(`.gitignore`)
  로컬 DB가 적재된 컴퓨터에서만 덤프를 만들 수 있습니다.

## 4. 인프라 요약

| 구성 | 값 |
|---|---|
| 배포 방식 | 단일 EC2 + docker compose (Caddy + API), 코드/사유는 [배포_계획.md D1](docs/배포_계획.md) |
| EC2 | t4g.medium (ARM/Graviton), Ubuntu 24.04 LTS arm64, EBS gp3 30GB, 보안그룹 `paperai-web`(80·443·22) |
| DB | RDS PostgreSQL 17.x, db.t4g.medium, 단일 AZ, 퍼블릭 액세스 아님, 보안그룹 `paperai-db`(5432 ← `paperai-web`) |
| 도메인/DNS | Cloudflare, **DNS only(회색 구름)** — 절대 프록시 켜지 말 것 |
| 메일 | SES SMTP, `ap-northeast-2`, **샌드박스**(프로덕션 액세스 거절됨) — 발신자 검증 ID 방식 |
| 프론트 | 같은 도메인에서 서빙 (`frontend-dist/`), Caddy가 `/api/*`만 백엔드로 프록시 |

인스턴스 사이징·비용·각 결정의 대안 비교는 [docs/배포_계획.md](docs/배포_계획.md)
§2(결정 목록)·§3(비용)을 보세요.

## 5. 다른 PC에서 이어받을 때

git이 안 가져다주는 것들 (전부 `.gitignore`):

- **코퍼스 덤프** (`data/export/*.dump`, ~458MB) — 로컬 DB가 적재된 PC에서만 뜰 수 있음
- **AICE-FE의 `.env`** — `VITE_GOOGLE_CLIENT_ID`가 여기에만 있음
- **AWS CLI 자격증명**, **SSH 키페어(`.pem`)**, **`gh` CLI 인증** (org 권한 필요)

## 6. 참고

- [docs/배포_계획.md](docs/배포_계획.md) — 왜 이렇게 설계했는지 (§0~8), 그리고 2026-08-11까지의
  진행 이력 (§9, 동결).
- [CHANGELOG.md](CHANGELOG.md) — 이 배포 이후 코드에 어떤 기능이 추가/변경됐는지.
