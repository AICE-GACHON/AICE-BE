"""rate limit — 두 종류를 구분해서 쓴다.

**IP 기준**(`limiter.limit(...)` 기본): 인증 없이 열려 있는 엔드포인트
(가입/로그인/구글/refresh/온보딩)와 비로그인 공개 조회(`/story`). 아직 계정이
없으니 IP가 유일하게 셀 수 있는 단위다.

**사용자 기준**(`key_func=user_or_ip`): 로그인해야 부를 수 있지만 **호출마다 돈이
나가는** 엔드포인트 (PDF 업로드 → Haiku 비전, 분석 시작 → Sonnet 재정렬+종합).
여기서 IP로 세면 한 사람이 IP만 바꿔가며 예산을 비울 수 있고, 반대로 학교/회사
NAT 뒤의 여러 사용자가 서로의 몫을 잡아먹는다. 인증이 이미 끝난 경로이므로
계정을 세는 것이 맞다.

⚠️ 리버스 프록시 뒤에 둘 거면 .env에 TRUST_PROXY_HEADERS=1을 넣어야 한다. 안 넣으면
모든 요청의 IP가 프록시 주소로 보여서 IP 기준 상한이 **서비스 전체 상한**이 된다
(로그인 30/hour → 전 사용자 합쳐 시간당 30회). 아래 client_ip 참고.

⚠️ 저장소 기본값은 **프로세스 메모리**다. `uvicorn --workers N`으로 띄우면 워커마다
따로 세서 실제 상한이 N배가 된다. 워커를 늘릴 거면 .env에 RATE_LIMIT_STORAGE_URI로
redis://... 를 넣어야 상한이 실제 상한이 된다.
"""
import logging

from jose import JWTError
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings
from app.core.security import decode_token

log = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    """상한을 셀 실제 클라이언트 IP.

    `request.client.host`는 **직전 홉**의 주소다. 리버스 프록시 뒤에서는 그게 늘
    프록시라, IP 기준 상한이 전부 한 바구니로 뭉친다 — 로그인 30/hour가 서비스
    전체 30회가 되어 정상 사용자가 429를 맞는다. 그래서 TRUST_PROXY_HEADERS=1이면
    X-Forwarded-For를 본다.

    **오른쪽 끝 값을 쓴다.** 이 헤더는 클라이언트가 보낸 값에 프록시가 자기가 본
    주소를 덧붙이는 구조라(nginx `$proxy_add_x_forwarded_for`, ALB 동일), 왼쪽
    항목들은 클라이언트가 지어낼 수 있다. 우리가 믿을 수 있는 것은 **우리 프록시가
    적은 마지막 항목**뿐이다 — 프록시가 하나라는 전제이고, 여러 단이면 그만큼
    안쪽으로 세야 한다.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip()
    return get_remote_address(request)


def user_or_ip(request: Request) -> str:
    """로그인했으면 user_id, 아니면 요청 IP를 키로 쓴다.

    Depends로 풀린 User를 볼 수 없는 자리(키 함수는 라우터보다 먼저 돈다)라
    Authorization 헤더의 토큰을 여기서 직접 검증한다. **서명 검증까지 하므로**
    위조 토큰으로 남의 몫을 소진시키거나, 매 요청 임의의 sub를 넣어 상한을
    우회하는 것은 불가능하다. 검증에 실패하면 IP로 떨어진다 — 그 요청은 어차피
    라우터의 get_current_user에서 401이 된다.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = decode_token(token)
            if payload.get("type") == "access" and payload.get("sub"):
                return f"user:{payload['sub']}"
        except JWTError:
            pass
    return f"ip:{client_ip(request)}"


_storage_uri = settings.RATE_LIMIT_STORAGE_URI or None
if _storage_uri:
    log.info("rate limit 저장소: 외부 (워커 간 공유)")
else:
    log.info("rate limit 저장소: 프로세스 메모리 — 워커를 늘리면 상한이 워커 수만큼 커집니다")

# ⚠️ **key_style="endpoint"를 지우지 마세요. 지우면 상한이 조용히 사라집니다.**
#
# slowapi의 기본값은 "url"이고, 그 모드에서는 바구니 키가 **요청 URL 전체**다
# (slowapi/extension.py의 `_endpoint_key = endpoint_url if ...`). 경로 파라미터가
# 있는 라우트에서는 URL이 요청마다 달라지므로 바구니도 매번 새로 생긴다 —
# `/api/papers/1/story`와 `/api/papers/2/story`가 각각 30회를 따로 센다.
#
# 그래서 2026-08-17까지 **경로 파라미터가 있는 상한은 하나도 작동하지 않았다.**
# 실측으로 `/story`·`/revisions`에 40회를 던져도 429가 0건이었다(로그인처럼 URL이
# 고정된 엔드포인트만 정상 동작했다). 코드에 숫자가 적혀 있어서 아무도 눈치채지
# 못하는 종류의 실패다.
#
# "endpoint"는 뷰 함수 이름을 키로 쓰므로 같은 라우트의 모든 요청이 한 바구니에
# 모인다. 공유 링크(`/api/shared/{token}`)에는 이게 특히 중요하다 — 토큰 대입은
# 정의상 매 요청이 다른 URL이라, "url" 모드에서는 상한이 **영원히** 걸리지 않는다.
limiter = Limiter(key_func=client_ip, storage_uri=_storage_uri, key_style="endpoint")
