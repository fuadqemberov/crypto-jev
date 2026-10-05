"""Real async limiter, shared provider outage handling and cancellation regressions."""
import asyncio
from email.utils import formatdate
import time
import httpx
import pytest
from app.config import Settings
from app.jev import Jev, JevError
from test_analysis import answer


def test_concurrent_workers_are_paced_without_a_startup_burst():
    async def run():
        arrivals = []
        def handler(request):
            arrivals.append(time.monotonic())
            return httpx.Response(200, json=answer())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            jev = Jev(client, Settings(api_key='test', jev_requests_per_second=20.))
            await asyncio.gather(*(jev.evaluate({}) for _ in range(3)))
            assert len(arrivals) == 3
            assert all(b-a >= .045 for a,b in zip(arrivals, arrivals[1:]))
    asyncio.run(run())


@pytest.mark.parametrize('status,headers,first_code', [
    (401, {}, 'http_401'), (403, {}, 'http_403'),
    (429, {'Retry-After':'60'}, 'retry_after'),
    (503, {'Retry-After':formatdate(time.time()+120, usegmt=True)}, 'retry_after'),
])
def test_provider_failure_stops_other_symbols_without_http_calls(status, headers, first_code):
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(status, headers=headers, text='private-body')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            jev = Jev(client, Settings(api_key='private-key'))
            with pytest.raises(JevError) as failure: await jev.evaluate({})
            assert failure.value.code == first_code
            for _ in range(10):
                with pytest.raises(JevError) as blocked: await jev.evaluate({})
                assert blocked.value.code == 'cooldown'
            assert len(requests) == 1
            assert jev.health()['suppressed'] == 10
            assert jev.health()['cooldown_seconds'] > 30
            assert 'private' not in str(jev.health()) + str(failure.value)
    asyncio.run(run())


def test_cancelled_limiter_wait_does_not_send_request():
    async def run():
        seen=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (seen.append(r) or httpx.Response(200,json=answer())))) as client:
            jev=Jev(client,Settings(api_key='test',jev_requests_per_second=1.))
            await jev.evaluate({})
            pending=asyncio.create_task(jev.evaluate({}))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError): await pending
            assert len(seen)==1
    asyncio.run(run())


@pytest.mark.parametrize('rate', [0., -1., 21., float('nan'), float('inf'), True])
def test_invalid_request_rate_fails_startup(rate):
    with pytest.raises(ValueError): Settings(jev_requests_per_second=rate)
