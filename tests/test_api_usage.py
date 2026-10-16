import asyncio
import json

import httpx
import pytest

from studio import api_usage


@pytest.fixture
def usage_store(tmp_path, monkeypatch):
    monkeypatch.setattr(api_usage, '_usage_dir', lambda: tmp_path / 'usage')
    return tmp_path / 'usage'


def test_normalizes_chat_and_responses_usage_shapes():
    chat = api_usage.normalize_usage({'usage': {
        'prompt_tokens': 100, 'prompt_cache_hit_tokens': 20,
        'prompt_cache_miss_tokens': 80, 'completion_tokens': 50,
        'completion_tokens_details': {'reasoning_tokens': 12}, 'total_tokens': 150,
    }})
    assert chat == {'input_tokens': 100, 'cache_hit_tokens': 20, 'cache_miss_tokens': 80,
                    'output_tokens': 50, 'reasoning_tokens': 12, 'total_tokens': 150, 'known': True}
    responses = api_usage.normalize_usage({'usage': {
        'input_tokens': 20, 'input_tokens_details': {'cached_tokens': 5},
        'output_tokens': 9, 'output_tokens_details': {'reasoning_tokens': 4}, 'total_tokens': 29,
    }})
    assert responses['cache_hit_tokens'] == 5
    assert responses['cache_miss_tokens'] == 15
    assert responses['reasoning_tokens'] == 4
    missing = api_usage.normalize_usage({})
    assert missing['known'] is False
    assert missing['total_tokens'] is None


def test_cost_uses_deepseek_cny_peak_and_offpeak_rates(monkeypatch):
    monkeypatch.setenv('PPT_STUDIO_CN_HOLIDAYS_COMPLETE_YEARS', '2026')
    usage = {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 20,
             'prompt_cache_miss_tokens': 80, 'completion_tokens': 50, 'total_tokens': 150}
    peak = api_usage.calculate_cost('deepseek-flash', '2026-01-06T02:00:00+00:00', usage)
    offpeak = api_usage.calculate_cost('deepseek-flash', '2026-01-06T04:00:00+00:00', usage)
    assert peak['cost_cny'] == pytest.approx(0.000561)
    assert peak['price_period'] == 'peak'
    assert offpeak['cost_cny'] == pytest.approx(0.000280)
    monkeypatch.setenv('PPT_STUDIO_CN_HOLIDAYS', '2026-01-06')
    holiday = api_usage.calculate_cost('deepseek-flash', '2026-01-06T02:00:00+00:00', usage)
    assert holiday['price_period'] == 'off_peak_chinese_holiday'
    assert holiday['cost_cny'] == pytest.approx(0.000280)
    monkeypatch.delenv('PPT_STUDIO_CN_HOLIDAYS_COMPLETE_YEARS', raising=False)
    monkeypatch.delenv('PPT_STUDIO_CN_HOLIDAYS', raising=False)
    unverified = api_usage.calculate_cost('deepseek-flash', '2026-01-06T02:00:00+00:00', usage)
    assert unverified['cost_status'] == 'peak_rate_upper_bound_holiday_unverified'


def test_missing_usage_and_unrecognized_model_are_never_invented():
    stamp = '2026-10-15T02:00:00+00:00'
    assert api_usage.calculate_cost('deepseek-flash', stamp, {})['cost_cny'] is None
    unknown_model = api_usage.calculate_cost('custom-compatible-model', stamp, {
        'prompt_tokens': 1, 'prompt_cache_hit_tokens': 0,
        'prompt_cache_miss_tokens': 1, 'completion_tokens': 1,
    })
    assert unknown_model['cost_cny'] is None
    assert unknown_model['cost_status'] == 'unknown_model_rate'


def test_request_log_is_private_and_groups_by_task_slide_and_module(usage_store):
    token = api_usage.set_context(request_id='http-1', task_id='task-a', project_id='a' * 12,
                                  slide_id='S002', module='visual_agent')
    try:
        api_usage.record_request(model='deepseek-flash', messages=[
            {'role': 'user', 'content': 'TOP-SECRET-USER-TEXT'},
            {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,SECRET-IMAGE'}}]},
        ], response={'id': 'api-response-1', 'model': 'deepseek-flash', 'usage': {
            'prompt_tokens': 10, 'prompt_cache_hit_tokens': 2, 'prompt_cache_miss_tokens': 8,
            'completion_tokens': 5, 'total_tokens': 15,
        }}, duration_ms=123.4, retry_count=1)
    finally:
        api_usage.reset_context(token)
    log = (usage_store / 'requests.jsonl').read_text(encoding='utf-8')
    assert 'TOP-SECRET-USER-TEXT' not in log
    assert 'SECRET-IMAGE' not in log
    assert 'api_key' not in log.lower()
    data = api_usage.summary(project_id='a' * 12)
    assert data['request_count'] == 1
    assert data['by_slide'][0]['key'] == 'S002'
    assert data['by_module'][0]['key'] == 'visual_agent'
    assert data['by_task'][0]['key'] == 'task-a'
    assert data['totals']['total_tokens'] == 15
    assert data['totals']['retry_count'] == 1


def test_task_outcome_summary_reports_repair_rate_and_end_to_end_time(usage_store):
    api_usage.record_task_outcome(task_id='bench-1', project_id='e' * 12, slide_id='S001',
                                  module='visual_agent', status='candidate_ready', repair_success=True,
                                  duration_ms=12_000)
    api_usage.record_task_outcome(task_id='bench-2', project_id='e' * 12, slide_id='S002',
                                  module='visual_agent', status='requires_review', repair_success=False,
                                  duration_ms=18_000)
    result = api_usage.summary(project_id='e' * 12)['outcome_summary']
    assert result['measured_runs'] == 2
    assert result['successful_runs'] == 1
    assert result['repair_success_rate'] == 0.5
    assert result['duration_ms_avg'] == 15_000
    assert result['duration_ms_max'] == 18_000


def test_telemetry_storage_failure_does_not_raise_into_agent_flow(tmp_path, monkeypatch):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('occupied', encoding='utf-8')
    monkeypatch.setattr(api_usage, '_usage_dir', lambda: blocked)
    row = api_usage.record_request(model='deepseek-flash', messages=[], response={}, duration_ms=1)
    outcome = api_usage.record_task_outcome(task_id='safe-failure', status='complete')
    assert row['status'] == 'success'
    assert outcome['status'] == 'complete'


def test_clear_usage_records_truncates_only_the_usage_ledger(usage_store):
    usage_store.mkdir(parents=True)
    for name, rows in {
        'requests.jsonl': ['{}', '{"provider":"qwen"}'],
        'outcomes.jsonl': ['{}'],
        'transfers.jsonl': ['{}', '{}', '{}'],
    }.items():
        (usage_store / name).write_text('\n'.join(rows) + '\n', encoding='utf-8')
    unrelated = usage_store.parent / 'project.json'
    unrelated.write_text('{"keep":true}', encoding='utf-8')

    result = api_usage.clear_records()

    assert result == {'cleared': {'requests.jsonl': 2, 'outcomes.jsonl': 1, 'transfers.jsonl': 3}, 'total': 6}
    assert all(not (usage_store / name).read_text(encoding='utf-8')
               for name in ('requests.jsonl', 'outcomes.jsonl', 'transfers.jsonl'))
    assert unrelated.read_text(encoding='utf-8') == '{"keep":true}'


def test_usage_clear_api_is_project_scoped(usage_store, monkeypatch):
    from fastapi.testclient import TestClient
    from studio import server

    monkeypatch.setenv('PPT_STUDIO_USAGE_DIR', str(usage_store))
    api_usage._append('requests.jsonl', {'project_id': 'a' * 12, 'request_id': 'current'})
    api_usage._append('requests.jsonl', {'project_id': 'b' * 12, 'request_id': 'other'})
    response = TestClient(server.app).delete('/api/usage/records', params={'project_id': 'a' * 12})

    assert response.status_code == 200
    assert response.json()['total'] == 1
    assert [row['request_id'] for row in api_usage._read()] == ['other']


def test_anomaly_warnings_cover_large_input_repeat_issue_and_history_growth(usage_store):
    token = api_usage.set_context(task_id='task-loop', project_id='b' * 12,
                                  slide_id='S001', module='visual_agent', issue_id='issue-7')
    try:
        first = api_usage.record_request(model='deepseek-flash', messages=[{'role': 'user', 'content': 'same issue'}],
                                         response={'usage': {'prompt_tokens': 260000, 'prompt_cache_hit_tokens': 0,
                                             'prompt_cache_miss_tokens': 260000, 'completion_tokens': 1, 'total_tokens': 260001}})
        second = api_usage.record_request(model='deepseek-flash', messages=[{'role': 'user', 'content': 'same issue'}],
                                          response={'usage': {'prompt_tokens': 20, 'prompt_cache_hit_tokens': 0,
                                              'prompt_cache_miss_tokens': 20, 'completion_tokens': 1, 'total_tokens': 21}})
        api_usage.record_request(model='deepseek-flash', messages=[{'role': 'user', 'content': 'same issue'}],
                                 response={'usage': {'prompt_tokens': 20, 'prompt_cache_hit_tokens': 0,
                                     'prompt_cache_miss_tokens': 20, 'completion_tokens': 1, 'total_tokens': 21}})
        fourth = api_usage.record_request(model='deepseek-flash', messages=[{'role': 'user', 'content': 'same issue'}],
                                           response={'usage': {'prompt_tokens': 20, 'prompt_cache_hit_tokens': 0,
                                               'prompt_cache_miss_tokens': 20, 'completion_tokens': 1, 'total_tokens': 21}})
    finally:
        api_usage.reset_context(token)
    assert 'single_request_input_tokens_high' in first['anomalies']
    assert 'same_input_repeated_in_task' in second['anomalies']
    assert 'same_issue_multiple_model_calls' in fourth['anomalies']
    assert 'possible_same_issue_loop' in fourth['anomalies']

    token = api_usage.set_context(task_id='task-growth', module='planning')
    try:
        api_usage.record_request(model='deepseek-flash', messages=[{'role': 'user', 'content': 'x'}], response={})
        grown = api_usage.record_request(model='deepseek-flash', messages=[{'role': 'user', 'content': 'x' * 25001}], response={})
    finally:
        api_usage.reset_context(token)
    assert 'request_content_bytes_grew_sharply' in grown['anomalies']


def test_async_transport_records_real_usage_with_mock_response(monkeypatch, usage_store):
    from studio import ai
    original_client = httpx.AsyncClient
    body = {'id': 'mock-chat-id', 'model': 'deepseek-flash', 'choices': [{'message': {'content': '{"ok":true}'}}],
            'usage': {'prompt_tokens': 12, 'prompt_cache_hit_tokens': 2, 'prompt_cache_miss_tokens': 10,
                      'completion_tokens': 4, 'completion_tokens_details': {'reasoning_tokens': 1}, 'total_tokens': 16}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.client = original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        async def __aenter__(self):
            await self.client.__aenter__(); return self
        async def __aexit__(self, *args):
            return await self.client.__aexit__(*args)
        async def post(self, *args, **kwargs):
            return await self.client.post(*args, **kwargs)

    monkeypatch.setattr(ai.httpx, 'AsyncClient', FakeAsyncClient)
    token = api_usage.set_context(task_id='mock-async', project_id='c' * 12, slide_id='S003', module='formula_recognition')
    try:
        result = asyncio.run(ai.call_deepseek([{'role': 'user', 'content': 'mock'}], api_key='not-logged', json_mode=True))
    finally:
        api_usage.reset_context(token)
    assert result == {'ok': True}
    rows = [json.loads(line) for line in (usage_store / 'requests.jsonl').read_text().splitlines()]
    assert rows[0]['provider_response_id'] == 'mock-chat-id'
    assert rows[0]['usage']['input_tokens'] == 12
    assert rows[0]['usage']['reasoning_tokens'] == 1
    assert rows[0]['slide_id'] == 'S003'
    assert 'not-logged' not in (usage_store / 'requests.jsonl').read_text()


def test_sync_editing_provider_records_usage_and_keeps_tool_shape(monkeypatch, usage_store):
    from studio.visual_ppt_runtime import agent as runtime_agent
    original_client = httpx.Client
    body = {'id': 'mock-sync-id', 'model': 'deepseek-flash',
            'choices': [{'message': {'role': 'assistant', 'content': None,
                                     'tool_calls': [{'id': 'call-1', 'type': 'function'}]}}],
            'usage': {'prompt_tokens': 8, 'prompt_cache_hit_tokens': 1, 'prompt_cache_miss_tokens': 7,
                      'completion_tokens': 3, 'total_tokens': 11}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.client = original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        def __enter__(self):
            self.client.__enter__(); return self
        def __exit__(self, *args):
            return self.client.__exit__(*args)
        def post(self, *args, **kwargs):
            return self.client.post(*args, **kwargs)

    monkeypatch.setattr(httpx, 'Client', FakeClient)
    token = api_usage.set_context(task_id='mock-sync', project_id='d' * 12, slide_id='S004', module='editing_runtime')
    try:
        provider = runtime_agent.DeepSeekChatProvider(api_key='secret-never-log', model='deepseek-flash')
        result = provider.complete([{'role': 'user', 'content': 'mock'}], tools=[{'type': 'function'}])
    finally:
        api_usage.reset_context(token)
    assert result['tool_calls'][0]['id'] == 'call-1'
    row = json.loads((usage_store / 'requests.jsonl').read_text().splitlines()[0])
    assert row['provider_response_id'] == 'mock-sync-id'
    assert row['usage']['cache_hit_tokens'] == 1
    assert 'secret-never-log' not in (usage_store / 'requests.jsonl').read_text()


def test_async_json_retry_records_both_http_requests_and_failure_reason(monkeypatch, usage_store):
    from studio import ai
    original_client = httpx.AsyncClient
    responses = [
        {'id': 'mock-invalid', 'model': 'deepseek-flash', 'choices': [{'message': {'content': 'not-json'}}],
         'usage': {'prompt_tokens': 10, 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 10,
                   'completion_tokens': 2, 'total_tokens': 12}},
        {'id': 'mock-valid', 'model': 'deepseek-flash', 'choices': [{'message': {'content': '{"ok":true}'}}],
         'usage': {'prompt_tokens': 15, 'prompt_cache_hit_tokens': 2, 'prompt_cache_miss_tokens': 13,
                   'completion_tokens': 3, 'total_tokens': 18}},
    ]
    index = {'value': 0}
    def handler(request):
        body = responses[index['value']];index['value'] += 1
        return httpx.Response(200, json=body)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):self.client = original_client(transport=httpx.MockTransport(handler))
        async def __aenter__(self):await self.client.__aenter__();return self
        async def __aexit__(self, *args):return await self.client.__aexit__(*args)
        async def post(self, *args, **kwargs):return await self.client.post(*args, **kwargs)

    monkeypatch.setattr(ai.httpx, 'AsyncClient', FakeAsyncClient)
    try:
        result = asyncio.run(ai.call_deepseek([{'role': 'user', 'content': 'mock'}], api_key='ephemeral', json_mode=True))
    except Exception:
        # Restore the shared module patch before propagating a failure.
        raise
    assert result == {'ok': True}
    rows = [json.loads(line) for line in (usage_store / 'requests.jsonl').read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]['application_status'] == 'invalid_json_content'
    assert rows[1]['retry_count'] == 1
    assert rows[0]['usage']['total_tokens'] == 12
    assert 'ephemeral' not in (usage_store / 'requests.jsonl').read_text()


def test_streaming_helper_uses_final_usage_chunk_only(usage_store):
    stream = api_usage.StreamingRequestTelemetry(model='deepseek-flash', messages=[{'role': 'user', 'content': 'x'}])
    stream.feed('data: {"id":"stream-id","model":"deepseek-flash","choices":[{"delta":{"content":"hello"}}]}\n\n')
    final = 'data: {"id":"stream-id","model":"deepseek-flash","choices":[],"usage":{"prompt_tokens":3,"prompt_cache_hit_tokens":1,"prompt_cache_miss_tokens":2,"completion_tokens":1,"total_tokens":4}}\n\n'
    stream.feed(final[:53]);stream.feed(final[53:])
    row = stream.finish(duration_ms=75)
    assert row['provider_response_id'] == 'stream-id'
    assert row['usage']['total_tokens'] == 4
    assert row['application_status'] == 'stream_final_usage_received'
