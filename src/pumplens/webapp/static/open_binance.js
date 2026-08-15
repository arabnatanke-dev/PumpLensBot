// Try the native app and use Web as a safe fallback.
// Сначала пробуем приложение, затем безопасно открываем Web.
window.addEventListener("DOMContentLoaded", () => {
  const { appUrl, webUrl } = document.body.dataset;
  if (!appUrl || !webUrl) return;
  window.location.href = appUrl;
  window.setTimeout(() => window.location.replace(webUrl), 1400);
});
