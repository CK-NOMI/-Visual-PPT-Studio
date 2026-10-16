/* Shared shell and summary components used across the eight workspaces. */
(() => {
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);

  const AppShell = Object.freeze({
    projectItems(projects, activeId) {
      if (!projects.length) return '<p class="hint">还没有项目</p>';
      return projects.map((project) => `<div class="project-entry"><button class="project-item ${activeId === project.id ? "active" : ""}" data-pid="${escapeHtml(project.id)}"><strong>${escapeHtml(project.title)}</strong><small>${escapeHtml(project.pages)}页 · 设计图${escapeHtml(project.images)}张 · ${project.locked ? "已锁定" : "未锁定"}</small></button><button class="project-delete" data-delete-project="${escapeHtml(project.id)}" title="删除项目" aria-label="删除项目：${escapeHtml(project.title)}">×</button></div>`).join("");
    },
    setProjectContext(project) {
      document.getElementById("projectTitle").textContent = project.title;
      document.getElementById("projectSubtitle").textContent = `${String(project.topic || "").slice(0, 140)} · ${project.audience || "通用受众"} · ${project.slides.length}页`;
      document.getElementById("lockBadge").textContent = project.locked ? "● 逐页规划已锁定" : "○ 规划尚未锁定";
      const lockButton = document.getElementById("lockPlan");
      lockButton.textContent = project.locked ? "已锁定" : "锁定规划";
      lockButton.disabled = !!project.locked;
    },
    setActiveNavigation(tab) {
      document.querySelectorAll(".tabs [data-tab]").forEach((item) => item.classList.toggle("active", item.dataset.tab === tab));
      document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.id === `tab-${tab}`));
    }
  });

  const StatCard = Object.freeze({
    render(label, value) {
      return `<div class="stat"><strong>${escapeHtml(value)}</strong><small>${escapeHtml(label)}</small></div>`;
    }
  });
  window.StudioComponents = Object.freeze({ AppShell, StatCard });
})();
