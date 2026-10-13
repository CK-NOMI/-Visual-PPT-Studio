/* Shared SVG icon registry for the single page app. Keep icon artwork in assets/icons/*.svg. */
(() => {
  const icons = Object.freeze({
    brand: "brand-mark", plan: "plan", upload: "upload", qa: "qa",
    design: "design", editor: "editor", ocr: "ocr", batch: "batch", debug: "debug"
  });
  const iconUrl = (name) => `/static/assets/icons/${icons[name] || "plan"}.svg`;
  window.StudioIcons = Object.freeze({ icons, iconUrl });
})();
