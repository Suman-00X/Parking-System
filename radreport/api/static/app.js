/* The app shell's behaviour: the theme switch, the mobile navigation drawer, and dismissable notices.
 * Loaded on every page; it holds no page-specific logic. */

const THEME_KEY = "radreport-theme";

function readTheme() {
  try {
    return localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

applyTheme(readTheme());

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        /* private windows refuse storage; the switch still works for this page */
      }
    });
  });

  const body = document.body;
  document.querySelectorAll("[data-open-nav]").forEach((b) => b.addEventListener("click", () => body.classList.add("nav-open")));
  document.querySelectorAll("[data-close-nav]").forEach((b) => b.addEventListener("click", () => body.classList.remove("nav-open")));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") body.classList.remove("nav-open");
  });

  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".banner")?.remove());
  });

  // A notice in the query string has been shown once; drop it so a reload does not show it again.
  const url = new URL(location.href);
  if (url.searchParams.has("notice") || url.searchParams.has("error")) {
    url.searchParams.delete("notice");
    url.searchParams.delete("error");
    history.replaceState(null, "", url.pathname + (url.search || "") + url.hash);
  }

  document.querySelectorAll("[data-tabs]").forEach((group) => {
    const tabs = group.querySelectorAll("[role=tab]");
    tabs.forEach((tab) =>
      tab.addEventListener("click", () => {
        tabs.forEach((t) => {
          t.setAttribute("aria-selected", String(t === tab));
          const panel = document.getElementById(t.dataset.panel);
          if (panel) panel.hidden = t !== tab;
        });
      }),
    );
  });

  document.querySelectorAll("[data-fill-email]").forEach((button) =>
    button.addEventListener("click", () => {
      const email = document.getElementById("email");
      const password = document.getElementById("password");
      if (email) email.value = button.dataset.fillEmail;
      if (password) password.value = button.dataset.fillPassword;
      const lab = document.getElementById("lab");
      if (lab && button.dataset.fillLab) lab.value = button.dataset.fillLab;
      document.querySelector("[data-tabs] [role=tab]")?.click();
    }),
  );
});
