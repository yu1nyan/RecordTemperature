// Matter 機器の温度・湿度を読み続けて、PC 内の HTTP API（127.0.0.1 のみ）で返す常駐プログラム。
//
//   node matter/bridge.mjs
//
// 環境変数:
//   RECORDTEMP_DATA        データフォルダ（rtmatter.mjs と同じ。なければリポジトリの data/）
//   RTMATTER_PORT          HTTP のポート（既定 8767）
//   RTMATTER_PARENT_PID    親プロセスの ID。いなくなったら自分も終了する
//   RTMATTER_DEBUG         あれば matter.js のログを INFO にする
//
//   GET /health   … 接続状況
//   GET /devices  … 温湿度センサーの機器一覧と最新値
//
// ペアリング情報のフォルダは同時に2つのプロセスで使えない。ブリッジの起動中は rtmatter.mjs を実行しないこと。

import http from "node:http";
import {
    BasicInformationCluster,
    BridgedDeviceBasicInformationCluster,
    RelativeHumidityMeasurementCluster,
    TemperatureMeasurementCluster,
} from "@matter/main/clusters";
import { createController, endpointNumber, walk } from "./lib.mjs";

const PORT = Number(process.env.RTMATTER_PORT) || 8767;
const PARENT_PID = Number(process.env.RTMATTER_PARENT_PID) || 0;
const POLL_MS = 60_000;
const CLUSTER_TEMPERATURE = TemperatureMeasurementCluster.id;
const CLUSTER_HUMIDITY = RelativeHumidityMeasurementCluster.id;

function pad(n) { return String(n).padStart(2, "0"); }
function log(message) {
    const d = new Date();
    const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
        `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    console.log(`${stamp} ${message}`);
}

/** ノードごとの状態。sensors: キー "<エンドポイント>/<t|h>" → { number, kind, client, label, uniqueId, value, updated } */
const nodes = new Map();
let controller = null;
let server = null;
let shuttingDown = false;

async function safe(promise) {
    try { return await promise; } catch { return ""; }
}

/** ノードの構成（温度・湿度センサーのエンドポイント）を調べ直す */
async function scanStructure(state) {
    const { node } = state;
    const info = node.getRootClusterClient(BasicInformationCluster);
    if (info) {
        state.vendor = (await safe(info.getVendorNameAttribute())) || state.vendor;
        state.product = (await safe(info.getProductNameAttribute())) || state.product;
    }
    const found = new Map();
    for (const ep of walk(node.getDevices())) {
        const number = endpointNumber(ep);
        const bridged = ep.getClusterClient(BridgedDeviceBasicInformationCluster);
        let label = "", uniqueId = "";
        if (bridged) {
            label = (await safe(bridged.getNodeLabelAttribute?.())) || "";
            uniqueId = (await safe(bridged.getUniqueIdAttribute?.())) || "";
        }
        for (const [kind, cluster] of [["t", TemperatureMeasurementCluster], ["h", RelativeHumidityMeasurementCluster]]) {
            const client = ep.getClusterClient(cluster);
            if (!client) continue;
            const key = `${number}/${kind}`;
            const old = state.sensors.get(key);
            found.set(key, {
                number, kind, client, label, uniqueId,
                value: old?.value ?? null, updated: old?.updated ?? null,
            });
        }
    }
    state.sensors = found;
}

/** 温度と湿度のエンドポイントを組にして機器の一覧にする */
function buildDevices(state) {
    const byNumber = (a, b) => Number(a.number) - Number(b.number);
    const temps = [...state.sensors.values()].filter(s => s.kind === "t").sort(byNumber);
    const hums = [...state.sensors.values()].filter(s => s.kind === "h").sort(byNumber);
    const pairs = [];
    const restT = [], restH = [...hums];
    for (const t of temps) {
        const key = t.label || t.uniqueId;
        const i = key ? restH.findIndex(h => (h.label || h.uniqueId) === key) : -1;
        if (i >= 0) pairs.push([t, restH.splice(i, 1)[0]]);
        else restT.push(t);
    }
    // ラベルで組にできなかった残りは、番号順に順番に組にする
    const n = Math.max(restT.length, restH.length);
    for (let i = 0; i < n; i++) pairs.push([restT[i] ?? null, restH[i] ?? null]);
    pairs.sort((a, b) => Number((a[0] ?? a[1]).number) - Number((b[0] ?? b[1]).number));

    return pairs.map(([t, h], index) => {
        const label = t?.label || h?.label || "";
        let name = label || state.product || `ノード ${state.id}`;
        if (pairs.length > 1) name += ` ${index + 1}`;
        const updates = [t?.updated, h?.updated].filter(v => v != null);
        return {
            id: `${state.id}/${t ? t.number : "-"}/${h ? h.number : "-"}`,
            node: state.id,
            name,
            temperature: t?.value ?? null,
            humidity: h?.value ?? null,
            updated: updates.length ? Math.max(...updates) : null,
            connected: state.node.isConnected,
        };
    });
}

function setValue(state, number, kind, raw) {
    const sensor = state.sensors.get(`${number}/${kind}`);
    if (!sensor || typeof raw !== "number") return;
    sensor.value = raw / 100;
    sensor.updated = Math.floor(Date.now() / 1000);
}

async function pollNode(state) {
    for (const sensor of state.sensors.values()) {
        try {
            const raw = await sensor.client.getMeasuredValueAttribute();
            setValue(state, sensor.number, sensor.kind, raw);
        } catch (e) {
            log(`値の読み取りに失敗しました: ノード ${state.id} エンドポイント ${sensor.number}: ${e.message}`);
        }
    }
}

async function setupNode(nodeId) {
    const id = String(nodeId);
    const node = await controller.getNode(nodeId);
    const state = { id, node, vendor: "", product: "", sensors: new Map() };
    nodes.set(id, state);

    node.events.attributeChanged.on(data => {
        try {
            const { clusterId, endpointId, attributeName } = data.path;
            if (attributeName !== "measuredValue") return;
            if (clusterId === CLUSTER_TEMPERATURE) setValue(state, endpointId, "t", data.value);
            else if (clusterId === CLUSTER_HUMIDITY) setValue(state, endpointId, "h", data.value);
        } catch (e) {
            log(`値の更新でエラー: ${e.message}`);
        }
    });
    node.events.stateChanged.on(s => log(`ノード ${id} の状態: ${s}`));
    node.events.structureChanged.on(() => {
        scanStructure(state).then(() => pollNode(state)).catch(e => log(`構成の再取得に失敗: ${e.message}`));
    });

    node.connect();
    node.events.initializedFromRemote.once(async () => {
        try {
            await scanStructure(state);
            log(`接続しました: ノード ${id}（${state.vendor} ${state.product}）`);
            await pollNode(state);
        } catch (e) {
            log(`ノード ${id} の初期化でエラー: ${e.message}`);
        }
    });
}

function sendJson(res, status, body) {
    const text = JSON.stringify(body);
    res.writeHead(status, {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": Buffer.byteLength(text),
        "Cache-Control": "no-store",
    });
    res.end(text);
}

function handle(req, res) {
    try {
        const url = new URL(req.url, "http://127.0.0.1");
        if (req.method === "GET" && url.pathname === "/health") {
            sendJson(res, 200, {
                ok: true,
                nodes: [...nodes.values()].map(s => ({
                    node: s.id, vendor: s.vendor, product: s.product, connected: s.node.isConnected,
                })),
            });
        } else if (req.method === "GET" && url.pathname === "/devices") {
            sendJson(res, 200, { devices: [...nodes.values()].flatMap(buildDevices) });
        } else {
            sendJson(res, 404, { error: "not found" });
        }
    } catch (e) {
        log(`HTTP 処理でエラー: ${e.message}`);
        try { sendJson(res, 500, { error: "internal error" }); } catch { /* 無視 */ }
    }
}

async function shutdown(reason) {
    if (shuttingDown) return;
    shuttingDown = true;
    log(`終了します（${reason}）`);
    const force = setTimeout(() => process.exit(0), 10_000);
    try { server?.close(); server?.closeAllConnections?.(); } catch { /* 無視 */ }
    try { await controller?.close(); } catch (e) { log(`コントローラーを閉じる際にエラー: ${e.message}`); }
    clearTimeout(force);
    process.exit(0);
}

process.on("unhandledRejection", e => log(`予期しないエラー（Promise）: ${e?.stack ?? e}`));
process.on("uncaughtException", e => log(`予期しないエラー: ${e?.stack ?? e}`));
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));

if (PARENT_PID) {
    setInterval(() => {
        try { process.kill(PARENT_PID, 0); }
        catch (e) { if (e.code !== "EPERM") shutdown("親プロセスが終了しました"); }
    }, 5000);
}

// HTTP は先に起動する（ペアリング済みのノードがなくても動く）
server = http.createServer(handle);
server.on("error", e => {
    log(`HTTP サーバーのエラー: ${e.message}`);
    if (e.code === "EADDRINUSE") shutdown("ポートが使用中です");
});
server.listen(PORT, "127.0.0.1", () => log(`HTTP を開始しました: http://127.0.0.1:${PORT}`));

try {
    controller = createController();
    await controller.start();
    const nodeIds = controller.getCommissionedNodes();
    if (nodeIds.length === 0) log("ペアリング済みの機器がありません");
    for (const nodeId of nodeIds) {
        try { await setupNode(nodeId); }
        catch (e) { log(`ノード ${nodeId} に接続できません: ${e.message}`); }
    }
    setInterval(() => {
        for (const state of nodes.values()) {
            if (state.node.isConnected) pollNode(state).catch(e => log(`読み直しに失敗: ${e.message}`));
        }
    }, POLL_MS);
} catch (e) {
    log(`起動に失敗しました: ${e.message}`);
    await shutdown("起動失敗");
}
