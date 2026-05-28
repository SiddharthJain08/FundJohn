import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

from src.ingestion.edgar_client import EDGARClient


def _run(coro):
    return asyncio.run(coro)


def _async_cm(resp):
    """Build an object that works as `async with` returning resp."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def test_fetch_document_returns_bytes_on_200():
    async def go():
        async with EDGARClient() as c:
            with patch.object(c, '_session') as fake_session:
                resp = MagicMock()
                resp.status = 200
                resp.read = AsyncMock(return_value=b'<html>...8-K body...</html>')
                fake_session.get = MagicMock(return_value=_async_cm(resp))
                out = await c.fetch_document(
                    'https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/aapl-20260430.htm'
                )
                assert out == b'<html>...8-K body...</html>'
    _run(go())


def test_fetch_document_returns_none_on_404():
    async def go():
        async with EDGARClient() as c:
            with patch.object(c, '_session') as fake_session:
                resp = MagicMock()
                resp.status = 404
                resp.read = AsyncMock(return_value=b'')
                fake_session.get = MagicMock(return_value=_async_cm(resp))
                out = await c.fetch_document('https://www.sec.gov/Archives/edgar/data/X/Y/Z.htm')
                assert out is None
    _run(go())


def test_fetch_document_uses_user_agent_header():
    async def go():
        async with EDGARClient() as c:
            with patch.object(c, '_session') as fake_session:
                resp = MagicMock()
                resp.status = 200
                resp.read = AsyncMock(return_value=b'ok')
                fake_session.get = MagicMock(return_value=_async_cm(resp))
                await c.fetch_document('https://example.invalid/x.htm')
                args, kwargs = fake_session.get.call_args
                assert args[0] == 'https://example.invalid/x.htm'
    _run(go())
