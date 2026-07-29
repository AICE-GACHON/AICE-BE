"""인증 없이 열려 있는 엔드포인트(가입/로그인/구글/refresh/온보딩)용 rate limit.

키는 요청자 IP. 메모리 저장소라 프로세스가 여러 개면(멀티 워커) 워커별로 따로
세므로 완벽한 상한은 아니지만, 지금 배포 형태(단일 uvicorn 프로세스)에서는
스팸/브루트포스를 늦추는 데 충분하다. 나중에 워커를 늘리면 Redis 저장소로
바꿔야 한다 (slowapi는 storage_uri로 교체 가능).
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
