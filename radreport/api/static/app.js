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

  // Tabs a link can open: /features#hld opens a tab by its key, and #some-heading opens the tab holding it.
  document.querySelectorAll("[data-hash-tabs]").forEach((group) => {
    const tabs = [...group.querySelectorAll("[role=tab]")];
    const open = () => {
      const id = decodeURIComponent(location.hash.slice(1));
      if (!id) return;
      const byKey = tabs.find((t) => t.dataset.key === id);
      const target = byKey ? null : document.getElementById(id);
      const panel = target?.closest("[role=tabpanel]");
      const tab = byKey || tabs.find((t) => t.dataset.panel === panel?.id);
      if (!tab) return;
      if (tab.getAttribute("aria-selected") !== "true") tab.click();
      if (target) target.scrollIntoView();
      else group.scrollIntoView({ block: "start" });
    };
    tabs.forEach((tab) =>
      tab.addEventListener("click", (e) => {
        if (e.isTrusted) history.replaceState(null, "", "#" + tab.dataset.key);
      }),
    );
    window.addEventListener("hashchange", open);
    open();
  });

  // Recordings play while on screen and pause when scrolled away; the autoplay attribute alone is skipped for video below the fold.
  const videos = document.querySelectorAll("figure.media video");
  if (videos.length && "IntersectionObserver" in window) {
    const watch = new IntersectionObserver((entries) =>
      entries.forEach((entry) => (entry.isIntersecting ? entry.target.play().catch(() => {}) : entry.target.pause())),
    { threshold: 0.35 });
    videos.forEach((video) => watch.observe(video));
  }

  // Reason dialogs: the button opens one; the close button, Esc and a click on the backdrop shut it.
  document.querySelectorAll("[data-dialog-open]").forEach((button) =>
    button.addEventListener("click", () => document.getElementById(button.dataset.dialogOpen)?.showModal()),
  );
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (e) => {
      if (e.target === dialog) dialog.close();
    });
    dialog.querySelectorAll("[data-dialog-close]").forEach((b) => b.addEventListener("click", () => dialog.close()));
  });
});
