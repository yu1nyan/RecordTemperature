// Shared helpers
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}
function toast(msg, ms = 3000) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms);
}
function fmtTime(ts, withSec = false) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}` +
    (withSec ? `:${p(d.getSeconds())}` : "");
}
function fmtNum(v, digits = 1) { return v === null || v === undefined ? "--" : Number(v).toFixed(digits); }
const STATUS_LABEL = { ok: "正常", manual: "手入力", low_conf: "低信頼", invalid: "読取失敗",
  capture_error: "撮影失敗", uncalibrated: "未校正", dismissed: "確認済" };
function badge(s) { return `<span class="badge ${s}">${STATUS_LABEL[s] || s}</span>`; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function toLocalInput(ts) {
  const d = new Date(ts * 1000 - new Date().getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
}
function fromLocalInput(v) { return v ? Math.floor(new Date(v).getTime() / 1000) : null; }
const CAM_COLORS = [["#e4572e", "#2b7bd6"], ["#c2185b", "#00897b"], ["#f39c12", "#6a4fc9"], ["#8d6e63", "#0097a7"]];
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
