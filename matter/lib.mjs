// rtmatter.mjs（確認用 CLI）と bridge.mjs（常駐プログラム）で共通に使う部分。

import path from "node:path";
import { fileURLToPath } from "node:url";
import { Environment, Logger, LogLevel } from "@matter/main";
import { ManualPairingCodeCodec, QrPairingCodeCodec } from "@matter/main/types";
import { CommissioningController } from "@project-chip/matter.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const DATA_DIR = process.env.RECORDTEMP_DATA || path.join(HERE, "..", "data");
export const STORAGE_DIR = path.join(DATA_DIR, "matter");

// matter.js 自体のログは WARN 以上だけ（RTMATTER_DEBUG があれば INFO）
Logger.level = process.env.RTMATTER_DEBUG ? LogLevel.INFO : LogLevel.WARN;

export function createController() {
    const environment = Environment.default;
    environment.vars.set("storage.path", STORAGE_DIR);
    return new CommissioningController({
        environment: { environment, id: "recordtemp-controller" },
        autoConnect: false,
        adminFabricLabel: "RecordTemperature",
    });
}

/** ペアリングコード（11桁/21桁の数字、または QR コードの "MT:..."）から識別子とパスコードを取り出す */
export function parsePairingCode(code) {
    const text = code.trim();
    if (text.toUpperCase().startsWith("MT:")) {
        const [data] = QrPairingCodeCodec.decode(text);
        return { identifierData: { longDiscriminator: data.discriminator }, passcode: data.passcode };
    }
    const data = ManualPairingCodeCodec.decode(text.replace(/[^0-9]/g, ""));
    return { identifierData: { shortDiscriminator: data.shortDiscriminator }, passcode: data.passcode };
}

/** 機器のすべてのエンドポイントを、子（ブリッジの先の機器）も含めてたどる */
export function* walk(endpoints) {
    for (const ep of endpoints) {
        yield ep;
        yield* walk(ep.getChildEndpoints());
    }
}

export function endpointNumber(ep) {
    return ep.number ?? ep.getNumber?.() ?? "?";
}

export function withTimeout(promise, ms, message) {
    let timer;
    return Promise.race([
        promise,
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); }),
    ]).finally(() => clearTimeout(timer));
}
