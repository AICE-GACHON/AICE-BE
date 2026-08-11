# syntax=docker/dockerfile:1
#
# 배포용 API 이미지. 로컬 개발은 venv + docker-compose.yml(Postgres만)을 그대로 쓴다.
#
# **레이어 순서는 "잘 안 바뀌는 것부터"다.** 앱 코드가 맨 마지막에 오므로 코드를
# 고쳐 다시 빌드해도 torch(~200MB)와 모델 가중치(~440MB)는 캐시에서 온다. 순서를
# 바꾸면 한 글자 고칠 때마다 수백 MB를 다시 받는다.
#
# 최종 이미지는 4~5GB다. EC2에서 직접 빌드하면 레지스트리(ECR)가 필요 없다.

FROM python:3.13-slim

# CI가 지키는 버전과 같다(.github/workflows/ci.yml). README가 3.13+를 표방하는
# 한 이미지도 낮은 쪽을 쓴다 — 3.14는 어노테이션을 지연 평가해서 3.13에서
# import조차 안 되는 코드를 통과시킨다.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf

# ------------------------------------------------------------ 0) 실행 사용자
#
# 사용자를 **맨 먼저** 만든다. 예전에는 맨 끝에서 `chown -R app:app /opt/hf /app`을
# 했는데, chown은 파일을 수정하는 것이라 대상 전체가 새 레이어에 복제된다 —
# 모델 844MB + 앱 코드가 통째로 복사돼 **1.37GB짜리 레이어**가 생겼다.
#
# 대신 디렉터리가 비어 있을 때 소유권을 잡아두고, 이후 모델은 app 사용자로 받고
# 앱 코드는 COPY --chown으로 넣는다. chown -R은 어디에도 쓰지 않는다.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /opt/hf /app \
    && chown app:app /opt/hf /app

WORKDIR /app

# ------------------------------------------------------------ 1) torch (CPU 빌드)
#
# 반드시 requirements보다 **먼저**, CPU 인덱스에서 받는다. 순서를 바꾸면
# `torch>=2.9`가 기본 PyPI에서 CUDA 런타임까지 딸린 2GB+ 빌드를 끌어온다.
# 이 서비스는 GPU를 쓰지 않는다 (requirements.txt 상단 주석).
#
# 🔴 **이 핀은 requirements.lock.txt의 torch 줄과 항상 같이 움직여야 한다.**
#    `+cpu`는 PyPI에 없고 이 인덱스에만 있는 로컬 버전 태그다. 여기서 핀을 빼면
#    최신 torch가 들어오고, 다음 단계의 락이 없는 2.13.0+cpu로 다운그레이드를
#    시도하다 죽는다. 반대로 락만 올리고 여기를 안 고쳐도 같은 곳에서 죽는다.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0+cpu

# ------------------------------------------------------------ 2) 나머지 의존성
#
# 락파일만 먼저 복사한다 — 앱 코드가 바뀌어도 이 레이어는 캐시된다.
#
# **requirements.txt가 아니라 락을 쓴다.** requirements.txt는 의도적으로 하한(>=)만
# 두는데, 그러면 언제 빌드하느냐에 따라 다른 버전이 들어가서 "어제 되던 이미지가
# 오늘 안 된다"를 재현할 수 없다. 자동 배포에서는 이게 특히 아프다 — 코드를 안
# 건드린 배포가 의존성만으로 깨진다.
#
# 락은 2026-08-11 프로덕션 컨테이너에서 떴다. 갱신 방법은 그 파일 상단 주석에 있다.
# 여기서 requirements.txt를 같이 복사하지 않는 이유는 캐시다 — 하한을 한 줄
# 고칠 때마다 3)의 모델 440MB를 다시 받게 된다. 둘의 어긋남은 5)에서 검사한다.
COPY requirements.lock.txt ./
RUN pip install -r requirements.lock.txt

# ------------------------------------------------------------ 3) 모델 가중치 굽기
#
# SPECTER2를 이미지에 넣는다. 안 그러면 컨테이너가 뜰 때마다 Hugging Face에서
# 440MB를 받고, **HF 장애가 곧 우리 장애가 된다.**
#
# 여기서 저장소 상수 대신 literal을 쓰는 이유는 순전히 캐시 때문이다 — 앱 코드를
# COPY한 뒤에 받으면 코드 한 줄만 고쳐도 440MB를 다시 받는다. 값이 갈라질 위험은
# 아래 5)의 검사가 막는다.
#
# 로드 경로는 paper_assistant/embedding/specter2.py와 **똑같이** 맞춘다. base와
# adapter가 별개 저장소라, tokenizer만 받아두면 adapter에서 다시 네트워크를 탄다.
#
# 여기서 app 사용자로 내려온다 — 파일이 처음부터 app 소유로 만들어지므로 나중에
# chown할 일이 없다(0) 참고). 의존성은 root가 시스템 site-packages에 이미 깔았고,
# 앱 사용자가 그걸 수정할 수 없는 편이 오히려 안전하다.
USER app

RUN python - <<'PY'
from adapters import AutoAdapterModel
from transformers import AutoTokenizer

AutoTokenizer.from_pretrained("allenai/specter2_base")
model = AutoAdapterModel.from_pretrained("allenai/specter2_base")
model.load_adapter("allenai/specter2", source="hf", load_as="proximity", set_active=True)
print("모델 캐시 완료")
PY

# ------------------------------------------------------------ 4) 캐시 완전성 증명
#
# 네트워크를 끊은 상태로 한 번 더 로드한다. 런타임에도 HF_HUB_OFFLINE=1로 도는데,
# 캐시에 빠진 파일이 있으면 **배포된 뒤 첫 분석에서** 터진다. 그것을 빌드 시점으로
# 당긴다 — 여기서 실패하면 이미지가 아예 만들어지지 않는다.
RUN HF_HUB_OFFLINE=1 python - <<'PY'
from adapters import AutoAdapterModel
from transformers import AutoTokenizer

AutoTokenizer.from_pretrained("allenai/specter2_base")
model = AutoAdapterModel.from_pretrained("allenai/specter2_base")
model.load_adapter("allenai/specter2", source="hf", load_as="proximity", set_active=True)
print("오프라인 로드 확인 — 캐시가 완전하다")
PY

# ------------------------------------------------------------ 5) 앱 코드
#
# --chown으로 넣는다. 넣은 뒤에 chown하면 그만큼이 또 한 레이어가 된다.
COPY --chown=app:app . .

# 3)의 literal이 저장소 상수와 갈라지지 않았는지 검사한다. 모델 이름이 바뀌었는데
# 굽는 쪽을 안 고치면, 런타임에 HF_HUB_OFFLINE=1 상태로 없는 모델을 찾다가 죽는다.
# 그 사고를 빌드 실패로 바꾼다.
RUN python - <<'PY'
from paper_assistant.embedding.specter2 import ADAPTERS, BASE_MODEL

baked = {"base": "allenai/specter2_base", "proximity": "allenai/specter2"}
assert BASE_MODEL == baked["base"], (
    f"Dockerfile이 굽는 base({baked['base']})와 specter2.py의 BASE_MODEL"
    f"({BASE_MODEL})이 다르다. Dockerfile 3)·4) 단계를 함께 고칠 것.")
assert ADAPTERS["proximity"] == baked["proximity"], (
    f"Dockerfile이 굽는 adapter({baked['proximity']})와 specter2.py의 "
    f"ADAPTERS['proximity']({ADAPTERS['proximity']})가 다르다.")
print("모델 상수 일치 확인")
PY

# 락을 쓰면서 생긴 새 함정을 막는다: **requirements.txt에 패키지를 추가하고 락을
# 갱신하지 않으면 빌드는 멀쩡히 성공하고 런타임에 ImportError로 죽는다.** 2)가 더는
# requirements.txt를 보지 않기 때문이다. 그 사고를 여기서 빌드 실패로 바꾼다.
#
# 5)에 두는 것도 캐시 때문이다 — 2)에 두면 하한을 한 줄 고칠 때마다 모델을 다시 받는다.
#
# ⚠️ extras는 검사하지 못한다. `uvicorn[standard]`에서 uvloop이 빠져도 uvicorn
#    자체는 설치돼 있으므로 이 검사를 통과한다.
RUN python - <<'PY'
import importlib.metadata as md

from packaging.requirements import Requirement
from packaging.version import Version

drift = []
with open("requirements.txt", encoding="utf-8") as f:
    for raw in f:
        line = raw.split("#")[0].strip()
        if not line:
            continue
        req = Requirement(line)
        try:
            installed = md.version(req.name)
        except md.PackageNotFoundError:
            drift.append(f"{req.name}: 설치되지 않았다 (락에 없다)")
            continue
        # torch는 2.13.0+cpu처럼 로컬 버전 태그가 붙으므로 base_version으로 본다.
        if not req.specifier.contains(Version(installed).base_version, prereleases=True):
            drift.append(f"{req.name}: 설치된 {installed}이 '{req.specifier}'를 만족하지 않는다")

assert not drift, (
    "requirements.txt와 requirements.lock.txt가 어긋났다:\n  "
    + "\n  ".join(drift)
    + "\n\n락파일을 갱신할 것 — 방법은 requirements.lock.txt 상단 주석에 있다.")
print("requirements.txt ↔ 락파일 일치 확인")
PY

# ------------------------------------------------------------ 6) 기동
#
# 실행 사용자는 0)에서 이미 app으로 내려와 있다.
EXPOSE 8000

# 🔴 헬스체크는 **Host 헤더를 직접 붙여야 한다.**
#
# 앱의 TrustedHostMiddleware가 ALLOWED_HOSTS에 없는 Host를 400으로 막는데,
# 헬스체크는 127.0.0.1로 접속하므로 Host가 "127.0.0.1:8000"이 된다. 그냥 두면
# **모든 헬스체크가 400을 맞고 컨테이너가 영원히 unhealthy**가 된다 — 그리고
# 그건 배포 자동화가 멀쩡한 컨테이너를 죽이는 조건이다. 실제로 이 상태로
# 만들었다가 로그가 400으로 도배되는 것을 보고 잡았다.
#
# ⚠️ HEALTHCHECK_HOST 값이 ALLOWED_HOSTS 안에 있어야 한다
#    (.env.production.example 참고). 도메인만 넣고 localhost를 빼면 다시 깨진다.
ENV HEALTHCHECK_HOST=localhost

# start-period가 긴 이유: WARMUP_ON_STARTUP=1이면 기동 때 SPECTER2를 로드한다
# (docs/배포_계획.md D7). 가중치가 로컬에 있어 다운로드는 없지만 그래도 수십 초가
# 걸리고, 그 사이 unhealthy로 찍히면 역시 컨테이너가 죽는다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=3 \
    CMD python -c "import os,sys,urllib.request; req=urllib.request.Request('http://127.0.0.1:8000/', headers={'Host': os.environ.get('HEALTHCHECK_HOST','localhost')}); sys.exit(0 if urllib.request.urlopen(req, timeout=4).status == 200 else 1)"

# --workers 1 고정. rate limit 저장소가 프로세스 메모리라 워커를 늘리면 상한이
# 그 배수로 뻥튀기되고, 그 상한들이 LLM 예산의 방어선이다 (docs/배포_계획.md §1.4).
# 늘리려면 RATE_LIMIT_STORAGE_URI(Redis)가 **먼저** 와야 한다.
#
# uvicorn의 --proxy-headers는 일부러 켜지 않는다. 앱이 X-Forwarded-For를 직접
# 읽어 rate limit 키를 만들고(app/core/rate_limit.py), 그쪽이 진짜 클라이언트를
# 고르는 규칙을 이미 갖고 있다. 둘 다 켜면 같은 헤더를 두 군데서 다르게 해석한다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
