/* Single transport layer used by page actions. Existing FastAPI routes remain authoritative. */
(() => {
  async function request(url, options = {}) {
    let response;
    try { response = await fetch(url, options); }
    catch (error) {
      if (error instanceof TypeError) {
        throw new Error(`无法连接本地 VisualPPTStudio 服务（${location.origin}）。服务可能已停止，请重新启动应用后刷新；本次操作未提交。`);
      }
      throw error;
    }
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = { detail: raw.slice(0, 350) }; }
    if (!response.ok) {
      const detail = data.detail;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || `HTTP ${response.status}`));
    }
    return data;
  }
  const jsonRequest = (method) => (url, body = {}) => request(url, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  window.StudioAPI = Object.freeze({ request, post: jsonRequest("POST"), put: jsonRequest("PUT") });
})();
