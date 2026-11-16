// 快適温湿度チャート: 横軸=相対湿度, 縦軸=温度。背景に PMV 等値線・絶対湿度線・各ゾーンを描き、記録値を点で重ねる。
// PMV 条件: 気温=放射温度, 気流 0.15 m/s, 代謝 1.0 met, 着衣 夏 0.5 clo（正側）/ 冬 1.0 clo（負側）

// ISO 7730 PMV
function pmv(ta, tr, vel, rh, met, clo) {
  const pa = rh * 10 * Math.exp(16.6536 - 4030.183 / (ta + 235));
  const icl = 0.155 * clo, m = met * 58.15, mw = m;
  const fcl = icl <= 0.078 ? 1 + 1.29 * icl : 1.05 + 0.645 * icl;
  const hcf = 12.1 * Math.sqrt(vel), taa = ta + 273, tra = tr + 273;
  const tcla = taa + (35.5 - ta) / (3.5 * icl + 0.1);
  const p1 = icl * fcl, p2 = p1 * 3.96, p3 = p1 * 100, p4 = p1 * taa;
  const p5 = 308.7 - 0.028 * mw + p2 * Math.pow(tra / 100, 4);
  let xn = tcla / 100, xf = tcla / 50, hc = hcf, n = 0;
  while (Math.abs(xn - xf) > 0.00015 && n++ < 150) {
    xf = (xf + xn) / 2;
    hc = Math.max(hcf, 2.38 * Math.pow(Math.abs(100 * xf - taa), 0.25));
    xn = (p5 + p4 * hc - p2 * Math.pow(xf, 4)) / (100 + p3 * hc);
  }
  const tcl = 100 * xn - 273;
  const hl1 = 3.05e-3 * (5733 - 6.99 * mw - pa), hl2 = mw > 58.15 ? 0.42 * (mw - 58.15) : 0;
  const hl3 = 1.7e-5 * m * (5867 - pa), hl4 = 0.0014 * m * (34 - ta);
  const hl5 = 3.96 * fcl * (Math.pow(xn, 4) - Math.pow(tra / 100, 4)), hl6 = fcl * hc * (tcl - ta);
  return (0.303 * Math.exp(-0.036 * m) + 0.028) * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6);
}
const COMFORT = { vel: 0.15, met: 1.0, cloSummer: 0.5, cloWinter: 1.0 };
const cloFor = level => level >= 0 ? COMFORT.cloSummer : COMFORT.cloWinter;

// f(t) が単調増加のとき f(t)=target となる t を二分法で求める
function bisect(f, target, lo = -20, hi = 60) {
  for (let i = 0; i < 40; i++) { const mid = (lo + hi) / 2; f(mid) < target ? lo = mid : hi = mid; }
  return (lo + hi) / 2;
}
const pmvTemp = (level, rh) => bisect(t => pmv(t, t, COMFORT.vel, rh, COMFORT.met, cloFor(level)), level);

// 絶対湿度 [g/m³]
function absHumidity(t, rh) {
  const es = 6.1078 * Math.pow(10, 7.5 * t / (t + 237.3));
  return 216.7 * (rh / 100 * es) / (t + 273.15);
}
const ahTemp = (ah, rh) => rh <= 0 ? 200 : bisect(t => absHumidity(t, rh), ah, -40, 200);

const RH_STEPS = Array.from({ length: 101 }, (_, i) => i);
const PMV_LEVELS = [3, 2, 1, 0.5, -0.5, -1, -2, -3];
const comfortCurves = (() => {
  const pmvLines = PMV_LEVELS.map(lv => ({ level: lv, pts: RH_STEPS.filter(r => r % 5 === 0).map(r => [r, pmvTemp(lv, r)]) }));
  const ah = v => RH_STEPS.map(r => [r, ahTemp(v, r)]);
  const comfortRh = RH_STEPS.filter(r => r >= 40 && r <= 70);
  return {
    pmvLines, ah7: ah(7), ah11: ah(11),
    comfortTop: comfortRh.map(r => [r, pmvTemp(0.5, r)]),
    comfortBottom: comfortRh.map(r => [r, pmvTemp(-0.5, r)]),
    target: { summer: [pmvTemp(0.5, 40), pmvTemp(0.5, 60)], winter: [pmvTemp(-0.5, 40), pmvTemp(-0.5, 60)] },
  };
})();

const ZONE = {
  flu: "rgba(110,115,135,.30)", fluWarn: "rgba(110,115,135,.15)",
  mold: "rgba(90,140,90,.20)", mite: "rgba(170,120,70,.20)",
  comfort: "rgba(240,120,80,.32)", pmv: "rgba(220,70,70,.55)", pmvText: "#d9534f",
};

const comfortBackground = {
  id: "comfortBackground",
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales: { x, y } } = chart;
    const X = v => x.getPixelForValue(v), Y = v => y.getPixelForValue(v);
    const lo = y.min - 5, hi = y.max + 5, clampT = t => Math.max(lo, Math.min(hi, t));
    const path = pts => { ctx.beginPath(); pts.forEach(([r, t], i) => ctx[i ? "lineTo" : "moveTo"](X(r), Y(clampT(t)))); };
    const fill = (pts, color) => { path(pts); ctx.closePath(); ctx.fillStyle = color; ctx.fill(); };
    const text = (s, r, t, color, align = "center", size = 12, bold = false) => {
      ctx.font = `${bold ? "600 " : ""}${size}px system-ui, sans-serif`; ctx.fillStyle = color;
      ctx.textAlign = align; ctx.textBaseline = "middle"; ctx.fillText(s, X(r), Y(t));
    };
    const c = comfortCurves, muted = cssVar("--muted"), strong = cssVar("--text");
    ctx.save();
    ctx.beginPath(); ctx.rect(a.left, a.top, a.right - a.left, a.bottom - a.top); ctx.clip();

    // インフルエンザ: 絶対湿度 7 g/m³ 未満=警戒, 7〜11=要注意
    fill([[0, hi], ...c.ah7, [100, lo], [0, lo]], ZONE.flu);
    fill([...c.ah11, ...c.ah7.slice().reverse()], ZONE.fluWarn);
    // カビ: 湿度70%以上・20℃以上 / ダニ: 湿度60%以上・25℃以上
    fill([[70, 20], [100, 20], [100, hi], [70, hi]], ZONE.mold);
    fill([[60, 25], [100, 25], [100, hi], [60, hi]], ZONE.mite);
    // 快適域: 冬 PMV-0.5 〜 夏 PMV+0.5, 湿度 40〜70%
    fill([...c.comfortTop, ...c.comfortBottom.slice().reverse()], ZONE.comfort);

    // 絶対湿度線
    ctx.setLineDash([4, 4]); ctx.lineWidth = 1; ctx.strokeStyle = muted;
    for (const pts of [c.ah7, c.ah11]) { path(pts); ctx.stroke(); }
    ctx.setLineDash([]);
    // PMV 等値線
    ctx.strokeStyle = ZONE.pmv;
    for (const ln of c.pmvLines) {
      path(ln.pts); ctx.stroke();
      const t0 = ln.pts[0][1];
      if (t0 > y.min + 1 && t0 < y.max - 1) text(`PMV${ln.level > 0 ? "+" : ""}${ln.level}`, 1, t0 + 0.7, ZONE.pmvText, "left", 11);
    }

    // ラベル
    const lblT = (pts, r) => pts.find(p => p[0] === r)[1];
    text("快適温湿度", 55, (lblT(c.comfortTop, 55) + lblT(c.comfortBottom, 55)) / 2, strong, "center", 15, true);
    text(`夏の目標 ${c.target.summer[0].toFixed(1)}〜${c.target.summer[1].toFixed(1)}℃`, 55, lblT(c.comfortTop, 55) + 1, ZONE.pmvText, "center", 11);
    text(`冬の目標 ${c.target.winter[1].toFixed(1)}〜${c.target.winter[0].toFixed(1)}℃`, 55, lblT(c.comfortBottom, 55) - 1, ZONE.pmvText, "center", 11);
    if (y.max > 30) text("ダニ増殖", 80, Math.min(y.max - 2, 35), muted);
    text("カビ増殖", 92, 22, muted);
    text("絶対湿度 11g/m³", 99, clampT(lblT(c.ah11, 95)) + 1.2, muted, "right", 11);
    text("絶対湿度 7g/m³", 99, clampT(lblT(c.ah7, 95)) + 1.2, muted, "right", 11);
    text("インフルエンザ 要注意", 75, (lblT(c.ah11, 75) + lblT(c.ah7, 75)) / 2, muted, "center", 11);
    if (lblT(c.ah7, 85) - y.min > 4) text("インフルエンザ 警戒", 85, y.min + 2.5, muted, "center", 12);
    ctx.restore();
  },
};

function makeComfortChart(el) {
  const grid = cssVar("--border"), textColor = cssVar("--muted");
  return new Chart(el, {
    type: "scatter", data: { datasets: [] }, plugins: [comfortBackground],
    options: {
      animation: false, maintainAspectRatio: false, parsing: false,
      scales: {
        x: { type: "linear", min: 0, max: 100, grid: { color: grid }, ticks: { color: textColor, stepSize: 10, callback: v => v + "%" },
          title: { display: true, text: "相対湿度", color: textColor } },
        y: { type: "linear", min: 0, max: 40, grid: { color: grid }, ticks: { color: textColor, stepSize: 5, callback: v => v + "℃" },
          title: { display: true, text: "温度", color: textColor } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: items => items.length ? fmtTime(items[0].raw.ts) : "",
          label: it => `${it.dataset.label}: ${it.raw.y.toFixed(1)}℃ / ${it.raw.x.toFixed(0)}%（絶対湿度 ${absHumidity(it.raw.y, it.raw.x).toFixed(1)}g/m³）`,
        } },
      },
    },
  });
}

// series: [{label, color, rows:[{ts, temperature, humidity}]}]  古い点ほど薄く、最新点は大きく描く
function updateComfortChart(chart, series) {
  const all = series.flatMap(s => s.rows).filter(r => r.temperature !== null && r.humidity !== null);
  const ts = all.map(r => r.temperature);
  const tMin = Math.min(...ts, 0), tMax = Math.max(...ts, 40);
  chart.options.scales.y.min = tMin < 0 ? Math.floor((tMin - 2) / 5) * 5 : 0;
  chart.options.scales.y.max = tMax > 40 ? Math.ceil((tMax + 2) / 5) * 5 : 40;
  const withAlpha = (hex, a) => hex + Math.round(a * 255).toString(16).padStart(2, "0");
  chart.data.datasets = series.map(s => {
    const pts = s.rows.filter(r => r.temperature !== null && r.humidity !== null)
      .map(r => ({ x: r.humidity, y: r.temperature, ts: r.ts }));
    const n = pts.length;
    return {
      label: s.label, data: pts,
      pointRadius: pts.map((_, i) => i === n - 1 ? 7 : 3),
      pointBackgroundColor: pts.map((_, i) => withAlpha(s.color, i === n - 1 ? 1 : 0.15 + 0.6 * (n > 1 ? i / (n - 1) : 1))),
      pointBorderColor: pts.map((_, i) => i === n - 1 ? cssVar("--surface") : "transparent"),
      pointBorderWidth: pts.map((_, i) => i === n - 1 ? 2 : 0),
      pointHoverRadius: 6,
    };
  });
  chart.options.plugins.legend.display = series.length > 1;
  chart.update();
}
