# 노트북에서 시연하기 (완전 오프라인)

외부망 시연을 위해 **노트북 하나로 전부 돌리는** 방법. 인터넷·원격 DB 필요 없음 →
시연장 Wi-Fi가 죽어도 데모가 멈추지 않는다.

> 이 컴퓨터의 DB(43,515편)를 노트북으로 복사해서, 노트북이 검색·임베딩·DB를 전부
> 로컬에서 처리한다.

## 노트북 준비물

- **Docker Desktop** (pgvector 컨테이너용)
- **Python 3.13+** (이 저장소는 3.14에서 개발·확인됨)
- 이 저장소(git clone)
- **DB dump 파일**: `data/export/paper_assistant.dump` (462MB) — 이 컴퓨터에서 USB/파일전송으로 옮길 것

## 단계

```bash
# 1. 저장소 클론 (또는 복사)
git clone <이 저장소 URL>
cd AICE

# 2. dump 파일을 data/export/ 에 넣기
#    (이 컴퓨터의 data/export/paper_assistant.dump 를 옮겨서 같은 경로에 둔다)
#    data/ 는 git에 없으므로 수동 복사 필요

# 3. Python 환경
python -m venv .venv
.venv\Scripts\activate            # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
#    첫 실행 시 SPECTER2 모델(~440MB)이 HuggingFace에서 자동 다운로드됨 → 노트북도 인터넷 한 번은 필요

# 4. 환경변수
cp .env.example .env
#    JWT_SECRET_KEY만 아무 값으로 채우면 시연에는 충분하다

# 5. DB 기동 + 복원
docker compose up -d              # pgvector 컨테이너 (포트 5433)
bash scripts/restore_db.sh        # dump 복원 (PowerShell이면 아래 '수동 복원' 참고)

# 6. 서비스 테이블 생성 + 백엔드 실행
alembic upgrade head
uvicorn app.main:app --reload     # → http://localhost:8000 (Swagger /docs)
```

화면까지 보려면 별도 저장소 [AICE-FE](https://github.com/AICE-GACHON/AICE-FE)를
나란히 클론해 함께 띄웁니다 (백엔드 CORS에 5173이 이미 열려 있습니다).

```bash
git clone https://github.com/AICE-GACHON/AICE-FE.git
cd AICE-FE && npm install && npm run dev    # → http://localhost:5173
```

프론트의 `.env`에 `VITE_API_BASE_URL=http://localhost:8000`이 있어야 실제 백엔드를
호출합니다 — 비어 있으면 화면이 전부 mock 응답으로 돌아갑니다.

### PowerShell에서 수동 복원 (restore_db.sh 대신)

```powershell
docker exec -i paper-assistant-db psql -U paper -d postgres -c "DROP DATABASE IF EXISTS paper_assistant WITH (FORCE);" -c "CREATE DATABASE paper_assistant;"
docker exec -i paper-assistant-db pg_restore -U paper -d paper_assistant --no-owner < data/export/paper_assistant.dump
```

## 시연 전 체크리스트

- [ ] `docker compose ps` — 컨테이너 healthy
- [ ] 복원 검증: `papers=43515` 나오는지 (restore_db.sh가 자동 출력)
- [ ] 분석 한 번 미리 돌려보기 (첫 요청은 SPECTER2 로드로 수십 초).
      데모 서버는 기동 시 자동으로 미리 로드한다(백엔드 API를 쓸 거면 `.env`에
      `WARMUP_ON_STARTUP=1`).
- [ ] SPECTER2 모델이 이미 캐시됐는지 (`~/.cache/huggingface`) — 시연장 인터넷 없이 돌리려면 최소 한 번은 온라인에서 실행해 캐시해둘 것
- [ ] LLM 종합(`PAPER_ASSISTANT_USE_LLM=1`)은 크레딧을 씁니다. `ANTHROPIC_API_KEY`가
      있을 때만 켜고, 없으면 기본 무료 모드($0)로 시연

## 왜 원격 DB가 아니라 복사인가

- 어차피 노트북에 torch + SPECTER2 + 코드가 다 필요 → DB만 원격 둘 이점이 거의 없음
- Postgres를 인터넷에 직접 노출하는 건 보안 위험(스캔·공격) + 공유기 포트포워딩/공인 IP 필요(CGNAT면 불가)
- 완전 로컬이면 시연 중 네트워크 의존 0 → 가장 안정적
