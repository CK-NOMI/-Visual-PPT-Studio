"""Browser regression using mocked job endpoints; never starts a model request."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit
import urllib.request

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    base, pid = args.url.rstrip('/'), args.project
    def read(path):
        with urllib.request.urlopen(base + path) as response:
            return json.load(response)
    before = read('/api/usage/summary')['request_count']
    saved = read(f'/api/projects/{pid}/visual-agent/latest?slide_id=S001')
    assert saved, 'Use a project with a saved S001 diagnosis/candidate'
    key = f'visual-ppt-studio:visual-agent-run:{pid}:S001'
    data = {'run_id': 'a' * 32, 'running': True, 'offline': False, 'launches': 0,
            'stops': 0, 'launch_mode': 'lost_response'}
    token = 'b' * 32
    issues = [{'issue_id': f'i{i}', 'priority': 'P0', 'status': 'open',
               'description': f'批次回归检查 {i}'} for i in (1, 2)]
    def info():
        return {'run_id': data['run_id'], 'project_id': pid, 'slide_id': 'S001',
                'mode': 'restore', 'running': data['running'], 'result_token': token,
                'progress': {'phase': 'editing' if data['running'] else 'stopped',
                             'slide_id': 'S001', 'message': '后台整批修复', 'iteration': 1,
                             'max_rounds': 2, 'batch_issue_ids': ['i1', 'i2'],
                             'batch_size': 2, 'issues': issues}}
    errors = []
    with sync_playwright() as pw:
        paths = sorted((Path.home() / 'Library/Caches/ms-playwright').glob(
            'chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell'))
        browser = pw.chromium.launch(headless=True, executable_path=str(paths[-1]) if paths else None)
        context = browser.new_context(viewport={'width': 1500, 'height': 1050})
        def route_job(route):
            path = urlsplit(route.request.url).path
            if path.endswith('/active-run'):
                if data['offline']: return route.abort('internetdisconnected')
                return route.fulfill(json=info() if data['running'] else None)
            if '/runs/' in path:
                if data['offline']: return route.abort('internetdisconnected')
                if path.endswith('/stop'):
                    data['stops'] += 1
                    data['running'] = False
                    return route.fulfill(json={'stop_requested': True, 'reason': 'Mock stop'})
                return route.fulfill(json=info())
            if path.endswith('/run'):
                data['launches'] += 1
                data['running'] = True
                if data['launch_mode'] == 'conflict':
                    data['run_id'] = 'c' * 32
                    return route.fulfill(status=409, json={'detail': {
                        'message': '当前页已有任务', 'run_id': data['run_id']}})
                data['run_id'] = route.request.post_data_json['run_id']
                return route.abort('connectionreset')
            if path.endswith('/' + token):
                result = deepcopy(saved)
                result.update(token=token, run_id=data['run_id'], status='stopped', reason='Mock task stopped')
                return route.fulfill(json=result)
            return route.continue_()
        context.route(f'**/api/projects/{pid}/visual-agent/**', route_job)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script(f'sessionStorage.setItem("visualPPTStudio.qaProject",{json.dumps(pid)});'
                             f'localStorage.removeItem({json.dumps(key)});')
        def wait_running():
            page.locator('#repairAutoRun:disabled').wait_for()
            page.wait_for_function('()=>document.querySelectorAll("#visualAgentProgress .visual-agent-progress-row.active").length===2')
            assert page.locator('#visualAgentProgress').get_by_text('本批优化中', exact=True).count() == 2
            assert page.locator('[data-stop-visual-agent]').is_visible()
            assert page.evaluate('key=>localStorage.getItem(key)', key) == data['run_id']
        page.goto(base)
        wait_running()
        page.reload()
        wait_running()  # local run ID deliberately missing; discover by page.
        data['offline'] = True
        page.get_by_text('连接暂时中断，正在自动恢复后台进度', exact=True).wait_for()
        assert page.locator('#repairAutoRun').is_disabled()
        assert page.locator('[data-stop-visual-agent]').is_visible()
        assert page.evaluate('key=>localStorage.getItem(key)', key) == data['run_id']
        data['offline'] = False
        page.wait_for_function('()=>!document.querySelector("#visualAgentProgress").innerText.includes("连接暂时中断")')
        wait_running()
        page.locator('#visualAgentProgress').screenshot(path='/tmp/visual-agent-recovered-batch.png')
        page.locator('[data-stop-visual-agent]').click()
        page.locator('#repairAutoRun:enabled').wait_for()
        assert data['stops'] == 1 and data['launches'] == 0
        for mode in ('lost_response', 'conflict'):
            data['launch_mode'] = mode
            page.locator('#repairDiagnose').click()
            wait_running()
            page.reload()
            wait_running()
            page.locator('[data-stop-visual-agent]').click()
            page.locator('#repairDiagnose:enabled').wait_for()
        assert data['launches'] == 2, data
        assert not errors, errors
        browser.close()
    after = read('/api/usage/summary')['request_count']
    assert after == before, 'Browser regression must not add actual model requests'
    print(json.dumps({'refresh_and_lost_storage': 'passed', 'temporary_disconnect': 'passed',
                      'lost_start_response_and_conflict': 'passed', 'stop_after_refresh': 'passed',
                      'all_batch_rows_active': 'passed', 'uncaught_js_errors': errors,
                      'additional_model_requests': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
