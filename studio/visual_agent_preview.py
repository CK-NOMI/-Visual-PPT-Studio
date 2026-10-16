"""Persistent, isolated Chromium preview worker for Visual Agent iterations."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from queue import Queue

from . import core
from .text_layout import layout_project


class VisualAgentBrowserPreview:
    """Keep one local server, Chromium process, page, and font cache per run.

    The worker only writes a disposable project copy under core.DATA. It never
    writes the source project or the user's formal project.
    """

    def __init__(self, source_project: dict, slide_id: str, session: Path):
        self.source_project = source_project
        self.slide_id = slide_id
        self.session = Path(session)
        # Project discovery validates IDs as hexadecimal, so keep the clone ID
        # in the same namespace as normal project folders.
        self.temp_id = uuid.uuid4().hex[:12]
        self.temp_root = core.DATA / self.temp_id
        self.log_handle = None
        self.server = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.url = None
        self.started = False
        self._prepare()
        self._requests = Queue()
        self._thread = threading.Thread(target=self._run, name='visual-agent-chromium', daemon=True)
        self._thread.start()

    def _prepare(self):
        from .core import project_dir, safe_json_write

        self.temp_root.mkdir(parents=True, exist_ok=False)
        (self.temp_root/'.visual_agent_preview').write_text('temporary Chromium-only candidate; never show as a user project',encoding='utf-8')
        source_root = project_dir(self.source_project['id'])
        shutil.copytree(source_root / 'images', self.temp_root / 'images')
        (self.temp_root / 'build').mkdir(exist_ok=True)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        self.url = f'http://127.0.0.1:{port}'
        env = os.environ.copy()
        env['PPT_STUDIO_DATA'] = str(core.DATA)
        env.pop('PPT_PREVIEW_DISABLE', None)
        self.log_handle = (self.session / 'persistent_browser_preview.log').open('w', encoding='utf-8')
        root = Path(__file__).resolve().parents[1]
        self.server = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'studio.server:app', '--host', '127.0.0.1', '--port', str(port)],
            cwd=root, env=env, stdout=self.log_handle, stderr=self.log_handle)
        self.safe_json_write = safe_json_write
        self.root = root

    def _write_candidate(self, candidate):
        clone, _ = layout_project(json.loads(json.dumps(candidate, ensure_ascii=False)))
        clone['id'] = self.temp_id
        self.safe_json_write(self.temp_root / 'project.json', clone)
        return clone

    def _wait_server(self):
        import httpx
        for _ in range(160):
            if self.server.poll() is not None:
                raise RuntimeError('Persistent preview service exited; see persistent_browser_preview.log')
            try:
                if httpx.get(self.url + '/api/projects', timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        raise RuntimeError('Persistent preview service startup timed out')

    def _load_page(self):
        self.page.goto(self.url)
        self.page.wait_for_function(
            '()=>window.visualPPTStudioQA?.snapshot().project&&!window.visualPPTStudioQA.snapshot().projectLoading')
        # Preview clones are intentionally hidden from the user project list.
        # Load the isolated project by its internal API instead of selecting a
        # temporary project card that must not be exposed in the normal UI.
        self.page.evaluate('id=>window.visualPPTStudioQA.loadProject(id)',self.temp_id)
        self.page.wait_for_function(
            '()=>window.visualPPTStudioQA?.snapshot().project&&!window.visualPPTStudioQA.snapshot().projectLoading')
        self.page.locator('[data-tab="plan"]').click()
        self.page.locator(f'.plan-entry[data-sid="{self.slide_id}"]').click()
        self.page.wait_for_function(
            '(sid)=>{const s=window.visualPPTStudioQA.snapshot();return s.sid===sid&&!s.projectLoading&&s.elements.length===s.project.slides.find(p=>p.id===sid).elements.length}',
            arg=self.slide_id)
        advanced = self.page.locator('.nav-advanced')
        if advanced.count() and advanced.get_attribute('open') is None:
            advanced.locator('summary').click()
        self.page.locator('[data-tab="editor"]').click()
        self.page.wait_for_function('()=>document.querySelector("#stage").clientWidth>0')
        self.page.evaluate('async()=>{await document.fonts.ready;window.visualPPTStudioQA.alignBaselines();await new Promise(requestAnimationFrame)}')
        self.page.add_style_tag(content='.stage-el.selected{outline:none!important;box-shadow:none!important}')

    def _start_browser(self):
        from playwright.sync_api import sync_playwright

        self._wait_server()
        self.playwright = sync_playwright().start()
        cache = Path.home() / 'Library/Caches/ms-playwright'
        candidates = sorted(cache.glob('chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell'))
        executable = str(candidates[-1]) if candidates else None
        self.browser = self.playwright.chromium.launch(headless=True, executable_path=executable)
        self.context = self.browser.new_context(viewport={'width': 1920, 'height': 1080}, device_scale_factor=2)
        self.page = self.context.new_page()
        self._load_page()
        self.started = True

    def _render_on_worker(self, candidate: dict, label: str) -> dict:
        """Render candidate stage on the one thread that owns Playwright."""
        started = time.perf_counter()
        clone = self._write_candidate(candidate)
        if not self.started:
            self._start_browser()
        else:
            # Keep the live page and font cache. Update only the candidate page
            # data through the browser QA bridge; no app/server/Chromium reload.
            self.page.evaluate('async payload => await window.visualPPTStudioQA.applyVisualAgentCandidate(payload.project,payload.sid)',
                               {'project': clone, 'sid': self.slide_id})
        folder = self.session / 'iterations' / label
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / 'chromium.png'
        self.page.locator('#stage').screenshot(path=str(target))
        return {'label': label, 'chromium': f'iterations/{label}/chromium.png',
                'preview_sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'renderer': 'Chromium browser',
                'preview_only': True, 'persistent_browser': True,
                'render_seconds': round(time.perf_counter() - started, 3),
                'browser_version': self.browser.version}

    def _run(self):
        while True:
            item = self._requests.get()
            if item is None:
                break
            action, payload, future = item
            if future.set_running_or_notify_cancel():
                try:
                    if action=='render':
                        candidate,label=payload
                        result=self._render_on_worker(candidate,label)
                    elif action=='inspect':
                        result=self.page.evaluate('id=>window.visualPPTStudioQA.inspectVisualAgentElement(id)',payload)
                    else:
                        raise ValueError('Unknown persistent browser preview action')
                    future.set_result(result)
                except Exception as exc:
                    future.set_exception(exc)
        self._close_browser()

    def render(self, candidate: dict, label: str) -> dict:
        """Submit a preview to the persistent Playwright-owning thread."""
        future = Future()
        self._requests.put(('render',(candidate,label),future))
        return future.result(timeout=180)

    def inspect_element(self, element_id: str) -> dict:
        """Read rendered DOM/text visibility from the current Chromium page."""
        future=Future()
        self._requests.put(('inspect',element_id,future))
        return future.result(timeout=30)

    def _close_browser(self):
        # Shutdown can race a nested uvicorn/process teardown. Close each layer
        # independently so one already-disconnected Playwright channel does not
        # leak a worker-thread traceback or skip the remaining cleanup.
        for resource,method in ((self.context,'close'),(self.browser,'close'),(self.playwright,'stop')):
            if resource:
                try:getattr(resource,method)()
                except Exception:pass

    def close(self):
        if getattr(self, '_thread', None) and self._thread.is_alive():
            self._requests.put(None)
            self._thread.join(timeout=15)
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)
        if self.log_handle:
            self.log_handle.close()
        shutil.rmtree(self.temp_root, ignore_errors=True)
