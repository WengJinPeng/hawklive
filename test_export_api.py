from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import cloud_api
import export_api


class ExportServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.slot = patch.object(export_api, '_export_slot', threading.BoundedSemaphore(1))
        self.slot.start()
        self.addCleanup(self.slot.stop)
        previous = export_api.app.dependency_overrides.copy()
        self.addCleanup(lambda: setattr(export_api.app, 'dependency_overrides', previous))

    def response(self, builder):
        with patch.object(cloud_api, 'customer_excel_export', builder):
            return export_api.customer_excel_export(
                start=1.0, end=2.0, cleanroom_ids='room-a', device_ids='device-a',
                locale='zh-CN', user={'customer_id': 'customer-a'},
            )

    async def consume(self, response, *, fail_send=False):
        messages = []

        async def receive():
            return {'type': 'http.disconnect'}

        async def send(message):
            if fail_send:
                raise OSError('client disconnected')
            messages.append(message)

        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        return messages

    def test_anonymous_requests_require_existing_session_authentication(self):
        with patch.object(cloud_api, 'customer_excel_export') as builder:
            response = TestClient(export_api.app).get('/api/history/export-xlsx')
        self.assertEqual(401, response.status_code)
        builder.assert_not_called()

    def test_only_health_and_export_routes_exist(self):
        client = TestClient(export_api.app)
        for path in ('/api/login', '/api/v1/readings/batch', '/api/v1/alarms/batch', '/api/config', '/docs', '/openapi.json'):
            with self.subTest(path=path):
                self.assertEqual(404, client.post(path, json={}).status_code)

    def test_authenticated_http_preserves_scope_customer_and_download_headers(self):
        export_api.app.dependency_overrides[cloud_api.customer_user] = lambda: {'customer_id': 'customer-a'}
        def build(**kwargs):
            self.assertEqual({'start': 1.0, 'end': 2.0, 'cleanroom_ids': 'room-a', 'device_ids': 'device-a', 'locale': 'zh-CN', 'user': {'customer_id': 'customer-a'}}, kwargs)
            return StreamingResponse(iter([b'PKtest']), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Language': 'zh-CN', 'Content-Disposition': 'attachment; filename=test.xlsx'})
        with patch.object(cloud_api, 'customer_excel_export', side_effect=build) as builder:
            client = TestClient(export_api.app)
            for _ in range(2):
                response = client.get('/api/history/export-xlsx', params={'start': 1, 'end': 2, 'cleanroom_ids': 'room-a', 'device_ids': 'device-a', 'locale': 'zh-CN', 'customer_id': 'other-customer'})
                self.assertEqual(200, response.status_code)
                self.assertEqual(b'PKtest', response.content)
                self.assertEqual('zh-CN', response.headers['content-language'])
                self.assertEqual('attachment; filename=test.xlsx', response.headers['content-disposition'])
            self.assertEqual(2, builder.call_count)

    async def test_busy_until_response_finishes_then_next_export_can_run(self):
        first = self.response(lambda **kwargs: StreamingResponse(iter([b'first'])))
        with self.assertRaises(HTTPException) as error:
            self.response(lambda **kwargs: self.fail('Busy request must not generate a workbook'))
        self.assertEqual(429, error.exception.status_code)
        self.assertEqual('5', error.exception.headers['Retry-After'])
        await self.consume(first)
        second = self.response(lambda **kwargs: StreamingResponse(iter([b'second'])))
        messages = await self.consume(second)
        self.assertEqual(b'second', b''.join(message.get('body', b'') for message in messages))

    async def test_generation_failure_releases_slot_and_preserves_original_error(self):
        def broken(**kwargs):
            raise HTTPException(413, 'Room exceeds original row limit')
        with self.assertRaises(HTTPException) as error:
            self.response(broken)
        self.assertEqual(413, error.exception.status_code)
        await self.consume(self.response(lambda **kwargs: StreamingResponse(iter([b'ok']))))

    async def test_stream_failure_releases_slot(self):
        async def broken_stream():
            yield b'first'
            raise RuntimeError('stream failed')
        with self.assertRaisesRegex(RuntimeError, 'stream failed'):
            await self.consume(self.response(lambda **kwargs: StreamingResponse(broken_stream())))
        await self.consume(self.response(lambda **kwargs: StreamingResponse(iter([b'ok']))))

    async def test_disconnected_client_releases_slot(self):
        with self.assertRaises(ClientDisconnect):
            await self.consume(self.response(lambda **kwargs: StreamingResponse(iter([b'ok']))), fail_send=True)
        await self.consume(self.response(lambda **kwargs: StreamingResponse(iter([b'ok']))))

    async def test_cancelled_response_releases_slot(self):
        waiting = asyncio.Event()
        async def stream():
            waiting.set()
            await asyncio.Event().wait()
            yield b'unreachable'
        response = self.response(lambda **kwargs: StreamingResponse(stream()))
        task = asyncio.create_task(self.consume(response))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.consume(self.response(lambda **kwargs: StreamingResponse(iter([b'ok']))))


if __name__ == '__main__':
    unittest.main()
