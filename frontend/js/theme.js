/* Presentation preference only; credentials and execution policy stay on the server. */
export function applyTheme(value = localStorage.getItem("agentos_theme") || "dark") {
  const choice = ["dark", "light", "system"].includes(value) ? value : "dark";
  document.documentElement.dataset.theme = choice === "system"
    ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : choice;
  localStorage.setItem("agentos_theme", choice);
  return choice;
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (localStorage.getItem("agentos_theme") === "system") applyTheme("system");
});
