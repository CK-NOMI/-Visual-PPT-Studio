"""Per-task, resumable spending controls shared by sync and async transports."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading


class APIPaused(RuntimeError):
    pass


_current = ContextVar('studio_api_budget', default=None)


@dataclass
class RequestBudget:
    max_cost_cny: float = 1.0
    max_requests: int = 12
    max_bytes: int = 64 * 1024 * 1024
    requests: int = 0
    content_bytes: int = 0
    cost_cny: float = 0.0
    unknown_cost_requests: int = 0
    paused_reason: str = ''
    _seen: dict = field(default_factory=dict)
    _lock: object = field(default_factory=threading.Lock, repr=False)

    def check(self):
        with self._lock:
            if not self.paused_reason and self.requests >= self.max_requests:
                self.paused_reason = f'已达到本次 {self.max_requests} 次 API 请求预算'
            if not self.paused_reason and self.cost_cny >= self.max_cost_cny:
                self.paused_reason = f'已达到本次 ¥{self.max_cost_cny:.2f} 费用预算（已知费用）'
            if not self.paused_reason and self.unknown_cost_requests >= 2:
                self.paused_reason = '已有两次请求费用未知，暂停以避免继续产生无法统计的开销'
            if self.paused_reason:
                raise APIPaused(self.paused_reason + '；候选会保留，可继续处理。')

    def before(self, fingerprint, size):
        with self._lock:
            if not self.paused_reason:
                if self.requests >= self.max_requests:
                    self.paused_reason = f'已达到本次 {self.max_requests} 次 API 请求预算'
                elif self.cost_cny >= self.max_cost_cny:
                    self.paused_reason = f'已达到本次 ¥{self.max_cost_cny:.2f} 费用预算（已知费用）'
                elif self.content_bytes + size > self.max_bytes:
                    self.paused_reason = '已达到本次传输量预算'
                elif self._seen.get(fingerprint, 0) >= 2:
                    self.paused_reason = '完全相同的请求已发送两次且没有新输入，暂停重复调用'
            if self.paused_reason:
                raise APIPaused(self.paused_reason + '；候选会保留，可继续处理。')
            self.requests += 1
            self.content_bytes += size
            self._seen[fingerprint] = self._seen.get(fingerprint, 0) + 1

    def before_upload(self, size):
        with self._lock:
            if self.content_bytes + size > self.max_bytes:
                self.paused_reason = '图片上传将超过本次传输量预算'
            if self.paused_reason:
                raise APIPaused(self.paused_reason + '；候选会保留，可继续处理。')
            self.content_bytes += size

    def after(self, row):
        with self._lock:
            cost = (row.get('cost') or {}).get('cost_cny')
            if cost is None:
                self.unknown_cost_requests += 1
            else:
                self.cost_cny += cost
            if row.get('http_status') in {401, 402, 403}:
                provider=row.get('provider') or 'deepseek'
                provider_name={'deepseek':'DeepSeek','qwen':'通义千问','glm':'智谱 GLM',
                               'opencode':'OpenCode'}.get(provider,provider)
                model=row.get('model')
                model_suffix=f'（{model}）' if model else ''
                self.paused_reason = f'{provider_name}{model_suffix} HTTP {row["http_status"]}，停止本次后续请求'

    def snapshot(self):
        return {k: getattr(self, k) for k in ('max_cost_cny', 'max_requests', 'max_bytes',
            'requests', 'content_bytes', 'cost_cny', 'unknown_cost_requests', 'paused_reason')}


def bind(budget):
    return _current.set(budget)


def reset(token):
    _current.reset(token)


def current():
    return _current.get()


def before_request(messages):
    budget = current()
    if budget is not None:
        budget.check()
        import json
        from .api_usage import _fingerprint
        budget.before(_fingerprint(messages), len(json.dumps(messages, ensure_ascii=False).encode()))
