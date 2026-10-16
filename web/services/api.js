/* Single transport layer used by page actions. Existing FastAPI routes remain authoritative. */
(() => {
  async function request(url, options = {}) {
    let response;
    try { response = await fetch(url, options); }
    catch (error) {
      if (error instanceof TypeError) {
        const unavailable = new Error(`暂时无法连接本地 VisualPPTStudio 服务（${location.origin}）。请求结果暂时无法确认，恢复连接后请查看任务状态。`);
        unavailable.status = 0;
        unavailable.transient = true;
        throw unavailable;
      }
      throw error;
    }
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = { detail: raw.slice(0, 350) }; }
    if (!response.ok) {
      const detail = data.detail;
      const failure = new Error(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail || `HTTP ${response.status}`));
      failure.status = response.status;
      failure.detail = detail;
      failure.transient = response.status >= 500 || response.status === 408 || response.status === 429;
      throw failure;
    }
    return data;
  }
  const jsonRequest = (method) => (url, body = {}) => request(url, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  window.StudioAPI = Object.freeze({ request, post: jsonRequest("POST"), put: jsonRequest("PUT") });
})();
