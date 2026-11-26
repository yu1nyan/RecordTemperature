// Matter 機器（SwitchBot ハブミニ Matter 対応など）から、温度・湿度を LAN 経由で読むための道具。
// この PC を Matter のコントローラーとして機器に追加（ペアリング）し、温度・湿度のクラスタを読む。
//
//   node matter/rtmatter.mjs discover                                   … ペアリング待ちの Matter 機器を LAN で探す
//   node matter/rtmatter.mjs pair <ペアリングコード> [機器のIPアドレス]   … 最初に1回だけ
//   node matter/rtmatter.mjs scan                                       … 機器の構成と温度・湿度を表示
//
// ペアリング情報（このコントローラーの鍵など）は RecordTemperature のデータフォルダの matter/ に保存する
// （環境変数 RECORDTEMP_DATA、なければリポジトリの data/）。
//
// matter.js 0.18 の CommissioningController（旧 API。0.19 で削除予定）を使うので、版は 0.18.0 に固定している。

import { Seconds } from "@matter/main";
import {
    BasicInformationCluster,
    GeneralCommissioning,
    RelativeHumidityMeasurementCluster,
    TemperatureMeasurementCluster,
} from "@matter/main/clusters";
import { createController, endpointNumber, parsePairingCode, walk, withTimeout } from "./lib.mjs";

/** ペアリング待ち（コミッショニング可能）の Matter 機器を mDNS で探して表示する */
async function discover() {
    const controller = createController();
    await controller.start();
    try {
        console.log("ペアリング待ちの Matter 機器を探しています（20秒）…");
        const found = await controller.discoverCommissionableDevices({}, { onIpNetwork: true }, undefined, Seconds(20));
        if (found.length === 0) {
            console.log("見つかりませんでした。");
        }
        for (const d of found) {
            const addresses = (d.addresses ?? []).map(a => `${a.ip}:${a.port}`).join(", ");
            console.log(`- ${d.DN ?? "(名前なし)"} VID=${d.V ?? "?"} PID=${d.P ?? "?"} 識別子=${d.D ?? "?"} ` +
                `ペアリング待ち=${d.CM ?? "?"} アドレス: ${addresses}`);
        }
    } finally {
        await controller.close();
    }
}

async function pair(code, ip) {
    if (!code) {
        throw new Error("ペアリングコードを指定してください: node matter/rtmatter.mjs pair <コード> [IPアドレス]");
    }
    const { identifierData, passcode } = parsePairingCode(code);
    const controller = createController();
    await controller.start();
    try {
        console.log("ペアリング中…（最大で1分ほどかかります）");
        const nodeId = await controller.commissionNode({
            commissioning: {
                regulatoryLocation: GeneralCommissioning.RegulatoryLocationType.IndoorOutdoor,
                regulatoryCountryCode: "JP",
            },
            discovery: {
                identifierData,
                discoveryCapabilities: { onIpNetwork: true },
                knownAddress: ip ? { ip, port: 5540, type: "udp" } : undefined,
            },
            passcode,
        });
        console.log(`ペアリングしました。ノード ID: ${nodeId}`);
        console.log("続けて `node matter/rtmatter.mjs scan` で温度・湿度が読めるか確認してください。");
    } finally {
        await controller.close();
    }
}

async function readNumber(client, label) {
    try {
        return await client.getMeasuredValueAttribute();
    } catch (e) {
        console.error(`  ${label} の読み取りに失敗: ${e.message}`);
        return null;
    }
}

async function scan() {
    const controller = createController();
    await controller.start();
    try {
        const nodeIds = controller.getCommissionedNodes();
        if (nodeIds.length === 0) {
            console.log("ペアリング済みの機器がありません。先に pair を実行してください。");
            return [];
        }
        const results = [];
        for (const nodeId of nodeIds) {
            const node = await controller.getNode(nodeId);
            node.connect();
            await withTimeout(node.events.initializedFromRemote, 60_000, `ノード ${nodeId} に接続できません`);

            const info = node.getRootClusterClient(BasicInformationCluster);
            const vendor = info ? await info.getVendorNameAttribute().catch(() => "") : "";
            const product = info ? await info.getProductNameAttribute().catch(() => "") : "";
            console.log(`\nノード ${nodeId}: ${vendor} ${product}`);

            for (const ep of walk(node.getDevices())) {
                const types = ep.getDeviceTypes().map(t => t.name).join(", ");
                const clusters = ep.getAllClusterClients().map(c => c.name).join(", ");
                console.log(`  エンドポイント ${endpointNumber(ep)} [${types}] 名前: ${ep.name ?? ""}`);
                console.log(`    クラスタ: ${clusters}`);
                const t = ep.getClusterClient(TemperatureMeasurementCluster);
                const h = ep.getClusterClient(RelativeHumidityMeasurementCluster);
                if (t || h) {
                    const tv = t ? await readNumber(t, "温度") : null;
                    const hv = h ? await readNumber(h, "湿度") : null;
                    // Matter の値は 0.01 単位の整数（温度 2234 = 22.34℃、湿度 4800 = 48.00%）
                    const temperature = tv == null ? null : tv / 100;
                    const humidity = hv == null ? null : hv / 100;
                    console.log(`    → 温度 ${temperature ?? "--"}℃ / 湿度 ${humidity ?? "--"}%`);
                    results.push({ nodeId: String(nodeId), endpoint: endpointNumber(ep), temperature, humidity });
                }
            }
            await node.disconnect();
        }
        if (results.length === 0) {
            console.log("\n温度・湿度のクラスタは見つかりませんでした（この機器は Matter に温湿度を公開していません）。");
        }
        return results;
    } finally {
        await controller.close();
    }
}

const [command, ...args] = process.argv.slice(2);
try {
    if (command === "pair") {
        await pair(args[0], args[1]);
    } else if (command === "scan") {
        await scan();
    } else if (command === "discover") {
        await discover();
    } else {
        console.log("使い方: node matter/rtmatter.mjs discover | pair <ペアリングコード> [IPアドレス] | scan");
        process.exitCode = 2;
    }
} catch (e) {
    console.error(`エラー: ${e.message}`);
    if (process.env.RTMATTER_DEBUG) console.error(e);
    process.exitCode = 1;
}
process.exit(process.exitCode ?? 0);
