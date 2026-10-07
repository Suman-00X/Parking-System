// Synthetic traffic for the hosted demo: keeps the free host awake and the dashboards fed.
//
//   k6 run ops/k6/synthetic.js -e BASE_URL=https://your-host \
//     -e ADMIN_EMAIL=... -e ADMIN_PASSWORD=... -e LAB=sunrise -e LAB_EMAIL=... -e LAB_PASSWORD=...
//
// Uses the read-only demo accounts only. Signs in once per run (setup), because sign-in is limited
// to 5 a minute per address and every virtual user shares one address. The mix stays under the
// per-address public limit (120/min) and the per-account read limits (300/min) at the default 3 users.

import http from "k6/http";
import { check, group, sleep } from "k6";

const BASE = (__ENV.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

export const options = {
  vus: Number(__ENV.VUS || 3),
  duration: __ENV.DURATION || "1m",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<1000"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export function setup() {
  const session = {};
  if (__ENV.ADMIN_EMAIL) {
    const res = http.post(`${BASE}/admin/login`, { email: __ENV.ADMIN_EMAIL, password: __ENV.ADMIN_PASSWORD }, { redirects: 0, tags: { name: "admin.login" } });
    const cookie = res.cookies.radreport_admin;
    check(res, { "admin sign-in": () => res.status === 303 && cookie && cookie.length > 0 });
    session.adminCookie = cookie && cookie.length ? cookie[0].value : null;
  }
  if (__ENV.LAB_EMAIL) {
    const res = http.post(`${BASE}/auth/login`, JSON.stringify({ lab: __ENV.LAB, email: __ENV.LAB_EMAIL, password: __ENV.LAB_PASSWORD }), { headers: { "Content-Type": "application/json" }, tags: { name: "auth.login" } });
    check(res, { "lab sign-in": (r) => r.status === 200 && !!r.json("access_token") });
    session.labToken = res.status === 200 ? res.json("access_token") : null;
  }
  return session;
}

export default function (session) {
  group("public", () => {
    const page = ["/recruiter", "/features", "/demo", "/api-docs"][__ITER % 4];
    const res = http.get(`${BASE}${page}`, { tags: { name: `public${page}` } });
    check(res, { "public page 200": (r) => r.status === 200 });
    check(http.get(`${BASE}/ready`, { tags: { name: "ops.ready" } }), { "ready 200": (r) => r.status === 200 });
  });

  if (session.adminCookie) {
    group("admin panel", () => {
      const res = http.get(`${BASE}/admin/labs`, { cookies: { radreport_admin: session.adminCookie }, redirects: 0, tags: { name: "admin.labs.page" } });
      check(res, { "admin labs 200": (r) => r.status === 200 });
    });
  }

  if (session.labToken) {
    group("lab", () => {
      const res = http.get(`${BASE}/review/queue`, { headers: { Authorization: `Bearer ${session.labToken}` }, tags: { name: "review.queue" } });
      check(res, { "review queue 200": (r) => r.status === 200 });
    });
  }

  sleep(3);
}

export function handleSummary(data) {
  const m = data.metrics;
  const ms = (v) => (v === undefined ? "n/a" : `${v.toFixed(0)} ms`);
  const lines = [
    "## Synthetic load",
    "",
    "| | |",
    "|---|---|",
    `| Requests | ${m.http_reqs.values.count} (${m.http_reqs.values.rate.toFixed(1)}/s) |`,
    `| Failed | ${(m.http_req_failed.values.rate * 100).toFixed(2)}% |`,
    `| Median | ${ms(m.http_req_duration.values.med)} |`,
    `| p95 | ${ms(m.http_req_duration.values["p(95)"])} |`,
    `| p99 | ${ms(m.http_req_duration.values["p(99)"])} |`,
    `| Checks passed | ${(m.checks.values.rate * 100).toFixed(2)}% |`,
    "",
  ].join("\n");
  return { stdout: lines + "\n", "k6-summary.json": JSON.stringify(data, null, 2), "k6-summary.md": lines };
}
