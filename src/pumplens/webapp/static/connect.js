// Never persist credentials in browser storage. / Никогда не сохраняем ключи в browser storage.
const telegram = window.Telegram?.WebApp;
telegram?.ready();
telegram?.expand();

const form = document.getElementById("connect-form");
const statusNode = document.getElementById("status");
const submitButton = document.getElementById("submit");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;
  statusNode.textContent = "Проверяем безопасные права…";

  const payload = {
    init_data: telegram?.initData ?? "",
    session_token: document.getElementById("session-token").value,
    csrf_token: document.getElementById("csrf-token").value,
    api_key: document.getElementById("api-key").value,
    secret_key: document.getElementById("secret-key").value,
    label: document.getElementById("label").value,
    read_only_confirmed: document.getElementById("read-only").checked,
  };

  try {
    const response = await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail ?? "connection_failed");
    statusNode.textContent = `Подключено. Ключ …${result.key_last4}`;
    form.reset();
    setTimeout(() => telegram?.close(), 1200);
  } catch (error) {
    statusNode.textContent = `Не удалось подключить: ${error.message}`;
  } finally {
    // Clear plaintext references as soon as possible. / Сразу очищаем plaintext-ссылки.
    payload.api_key = "";
    payload.secret_key = "";
    document.getElementById("api-key").value = "";
    document.getElementById("secret-key").value = "";
    submitButton.disabled = false;
  }
});
