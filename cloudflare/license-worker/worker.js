// Nano / Cashir activation Worker — license schema v2 + Telegram admin bot.
//
// Endpoints:
//   POST /activate            unchanged client contract, returns
//                             { signature, expirationDate, durationHours }
//   POST /telegram/webhook    Telegram admin bot (see commands below)
//   GET  /admin/licenses      listing (guarded by ADMIN_SECRET)
//   POST /admin/migrate       upgrade stored records to schema v2 (guarded)
//
// Telegram admin commands (only ids in TELEGRAM_ADMIN_IDS may run them):
//   /help                       show this help
//   /id                         show your Telegram id (bootstrap)
//   /list                       list every license
//   /stats                      totals
//   /add <code> [devices] [days]
//   /del <code>                 delete a license
//   /disable <code> | /enable <code>
//   /extend <code> <days>
//   /devices <code>             list registered devices
//   /reset <code> <fingerprint|all>
//
// Secrets (Dashboard -> Settings -> Variables and Secrets, or wrangler secret put):
//   SIGNING_KEY               RSA PKCS8 private key (PEM or base64) for /activate
//   ADMIN_SECRET              shared secret guarding /admin/*
//   TELEGRAM_TOKEN            bot token from @BotFather
//   TELEGRAM_ADMIN_IDS        comma-separated numeric Telegram user ids
//   TELEGRAM_WEBHOOK_SECRET   optional; must match the setWebhook secret_token
// KV binding:
//   LICENSES                  the existing namespace (id 9d830730022f40e0b60d0f060987f533)

const SCHEMA_VERSION = 2;
const DEFAULT_MAX_DEVICES = 330;
const YEAR_MS = 365 * 24 * 3600 * 1000;
const DURATION_HOURS = 8760;
const INDEX_KEY = "__index__";

// The allow-list lives in KV (the __index__ key + the license records
// themselves). An operator can also seed extra codes through the worker secret
// LEGACY_CODES (comma-separated) — no license code is ever hardcoded here, so
// this file is safe to keep in a public repository.
function envCodes(env) {
  return String(env?.LEGACY_CODES || "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

function maxDevicesFor(code, raw) {
  if (raw && Number.isFinite(raw.max_devices)) return raw.max_devices;
  return DEFAULT_MAX_DEVICES;
}

function upgradeRecord(code, raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const devices = Array.isArray(source.devices) ? source.devices : [];
  const now = Date.now();
  return {
    schema_version: SCHEMA_VERSION,
    code,
    edition: source.edition || "standard",
    features: Array.isArray(source.features) ? source.features : [],
    max_devices: maxDevicesFor(code, source),
    status: source.status || "active",
    expires_at: Number.isFinite(source.expires_at) ? source.expires_at : null,
    notes: source.notes || "",
    created_at: source.created_at ?? source.firstActivated ?? devices[0]?.firstSeen ?? null,
    firstActivated: source.firstActivated ?? devices[0]?.firstSeen ?? null,
    lastActivated: source.lastActivated ?? null,
    totalActivations: Number.isFinite(source.totalActivations) ? source.totalActivations : 0,
    activationAttempts: Number.isFinite(source.activationAttempts)
      ? source.activationAttempts
      : source.totalActivations ?? 0,
    devices: devices
      .filter((d) => d && typeof d.fingerprint === "string")
      .map((d) => ({
        fingerprint: d.fingerprint,
        firstSeen: d.firstSeen ?? now,
        lastSeen: d.lastSeen ?? d.firstSeen ?? now,
      })),
  };
}

async function generateSignature(data, env) {
  const raw = String(env?.SIGNING_KEY || "").replace(
    /-----BEGIN PRIVATE KEY-----|-----END PRIVATE KEY-----|\s/g,
    "",
  );
  if (!raw) throw new Error("SIGNING_KEY is not configured");
  const binaryKey = Uint8Array.from(atob(raw), (c) => c.charCodeAt(0));
  const privateKey = await crypto.subtle.importKey(
    "pkcs8",
    binaryKey,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    privateKey,
    new TextEncoder().encode(data),
  );
  return btoa(String.fromCharCode(...new Uint8Array(signature)));
}

async function kvGet(env, key) {
  try {
    return await env.LICENSES.get(key, "json");
  } catch {
    return null;
  }
}

async function kvPut(env, key, value) {
  try {
    await env.LICENSES.put(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

async function kvDelete(env, key) {
  try {
    await env.LICENSES.delete(key);
    return true;
  } catch {
    return false;
  }
}

async function listKvKeys(env) {
  const names = [];
  let cursor;
  do {
    const result = await env.LICENSES.list(cursor ? { cursor } : undefined);
    for (const entry of result.keys) {
      if (entry.name !== INDEX_KEY) names.push(entry.name);
    }
    cursor = result.list_complete ? null : result.cursor;
  } while (cursor);
  return names;
}

// Every code the system knows about: env seed + index + anything in KV.
async function allCodes(env) {
  const index = await kvGet(env, INDEX_KEY);
  const set = new Set(envCodes(env));
  for (const c of Array.isArray(index) ? index : []) set.add(c);
  for (const c of await listKvKeys(env)) set.add(c);
  return [...set].sort();
}

// Hot-path allow-list for /activate: the index if present, else the env seed
// plus every stored record key.
async function allowedCodes(env) {
  const index = await kvGet(env, INDEX_KEY);
  if (Array.isArray(index) && index.length) return index;
  const set = new Set(envCodes(env));
  for (const c of await listKvKeys(env)) set.add(c);
  return [...set];
}

async function writeIndex(env) {
  const codes = (await allCodes(env)).filter((c) => c !== INDEX_KEY);
  await kvPut(env, INDEX_KEY, codes);
  return codes;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// --------------------------------------------------------------------------
// Telegram
// --------------------------------------------------------------------------

function telegramAdmins(env) {
  return String(env?.TELEGRAM_ADMIN_IDS || "")
    .split(",")
    .map((v) => Number(v.trim()))
    .filter((v) => Number.isFinite(v) && v !== 0);
}

async function tgCall(env, method, payload) {
  const token = env?.TELEGRAM_TOKEN;
  if (!token) return null;
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await res.json();
  } catch {
    return null;
  }
}

function tgSend(env, chatId, text, keyboard) {
  const payload = { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true };
  if (keyboard) payload.reply_markup = { inline_keyboard: keyboard };
  return tgCall(env, "sendMessage", payload);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function fmtDate(ms) {
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toISOString().slice(0, 10);
}

function parseCommand(text) {
  const first = String(text || "").trim().split(/\s+/)[0] || "";
  const command = first.replace(/^\//, "").split("@")[0].toLowerCase();
  const args = String(text || "").trim().split(/\s+/).slice(1);
  return { command, args };
}

async function notifyAdmins(env, text) {
  for (const id of telegramAdmins(env)) {
    await tgSend(env, id, text);
  }
}

const HELP_TEXT = [
  "<b>لوحة تفعيل كاشير</b>",
  "/list — كل المفاتيح",
  "/stats — ملخص",
  "/add &lt;code&gt; [أجهزة] [أيام] — إنشاء مفتاح",
  "/del &lt;code&gt; — حذف",
  "/disable &lt;code&gt; / /enable &lt;code&gt;",
  "/extend &lt;code&gt; &lt;أيام&gt;",
  "/devices &lt;code&gt; — الأجهزة",
  "/reset &lt;code&gt; &lt;بصمة|all&gt;",
  "/id — معرّفك",
].join("\n");

async function handleTelegramCommand(env, chatId, text) {
  const { command, args } = parseCommand(text);

  if (command === "help" || command === "start") {
    await tgSend(env, chatId, HELP_TEXT);
    return;
  }

  if (command === "list") {
    const codes = await allCodes(env);
    if (!codes.length) {
      await tgSend(env, chatId, "لا توجد مفاتيح.");
      return;
    }
    const lines = ["<b>المفاتيح</b>"];
    for (const code of codes) {
      const rec = upgradeRecord(code, await kvGet(env, code));
      const status = rec.status === "active" ? "✅" : "⛔️";
      const exp = rec.expires_at ? ` · ينتهي ${fmtDate(rec.expires_at)}` : "";
      lines.push(
        `${status} <code>${escapeHtml(code)}</code> — أجهزة ${rec.devices.length}/${rec.max_devices}` +
          ` · تفعيلات ${rec.totalActivations}${exp}`,
      );
    }
    await tgSend(env, chatId, lines.join("\n"));
    return;
  }

  if (command === "stats") {
    const codes = await allCodes(env);
    let active = 0;
    let devices = 0;
    let activations = 0;
    for (const code of codes) {
      const rec = upgradeRecord(code, await kvGet(env, code));
      if (rec.status === "active") active += 1;
      devices += rec.devices.length;
      activations += rec.totalActivations;
    }
    await tgSend(
      env,
      chatId,
      `<b>ملخص</b>\nالمفاتيح: ${codes.length} (فعّالة ${active})\nالأجهزة: ${devices}\nالتفعيلات: ${activations}`,
    );
    return;
  }

  if (command === "add") {
    const code = (args[0] || "").trim();
    if (!code) {
      await tgSend(env, chatId, "الصيغة: /add &lt;code&gt; [أجهزة] [أيام]");
      return;
    }
    const existing = await kvGet(env, code);
    if (existing) {
      await tgSend(env, chatId, `المفتاح <code>${escapeHtml(code)}</code> موجود مسبقًا.`);
      return;
    }
    const devices = Number.isFinite(Number(args[1])) ? Number(args[1]) : DEFAULT_MAX_DEVICES;
    const days = Number.isFinite(Number(args[2])) ? Number(args[2]) : 0;
    const now = Date.now();
    const record = upgradeRecord(code, {
      devices: [],
      max_devices: devices,
      status: "active",
      created_at: now,
      expires_at: days > 0 ? now + days * 86400000 : null,
    });
    await kvPut(env, code, record);
    await writeIndex(env);
    await tgSend(
      env,
      chatId,
      `تم إنشاء <code>${escapeHtml(code)}</code> — أجهزة ${devices}` +
        (days > 0 ? ` · ينتهي ${fmtDate(record.expires_at)}` : ""),
    );
    return;
  }

  if (command === "del") {
    const code = (args[0] || "").trim();
    if (!code) {
      await tgSend(env, chatId, "الصيغة: /del &lt;code&gt;");
      return;
    }
    await kvDelete(env, code);
    await writeIndex(env);
    await tgSend(env, chatId, `تم حذف <code>${escapeHtml(code)}</code>.`);
    return;
  }

  if (command === "disable" || command === "enable") {
    const code = (args[0] || "").trim();
    const rec = code ? await kvGet(env, code) : null;
    if (!rec) {
      await tgSend(env, chatId, "المفتاح غير موجود.");
      return;
    }
    const record = upgradeRecord(code, rec);
    record.status = command === "disable" ? "disabled" : "active";
    await kvPut(env, code, record);
    await tgSend(
      env,
      chatId,
      `${command === "disable" ? "تم تعطيل" : "تم تفعيل"} <code>${escapeHtml(code)}</code>.`,
    );
    return;
  }

  if (command === "extend") {
    const code = (args[0] || "").trim();
    const days = Number(args[1]);
    const rec = code ? await kvGet(env, code) : null;
    if (!rec || !Number.isFinite(days) || days <= 0) {
      await tgSend(env, chatId, "الصيغة: /extend &lt;code&gt; &lt;أيام&gt;");
      return;
    }
    const record = upgradeRecord(code, rec);
    const base = Math.max(Date.now(), record.expires_at || 0);
    record.expires_at = base + days * 86400000;
    await kvPut(env, code, record);
    await tgSend(env, chatId, `تم التمديد إلى ${fmtDate(record.expires_at)}.`);
    return;
  }

  if (command === "devices") {
    const code = (args[0] || "").trim();
    const rec = code ? await kvGet(env, code) : null;
    if (!rec) {
      await tgSend(env, chatId, "المفتاح غير موجود.");
      return;
    }
    const record = upgradeRecord(code, rec);
    if (!record.devices.length) {
      await tgSend(env, chatId, "لا توجد أجهزة مسجّلة.");
      return;
    }
    const lines = [`<b>أجهزة ${escapeHtml(code)}</b> (${record.devices.length}/${record.max_devices})`];
    record.devices.slice(0, 50).forEach((d, i) => {
      lines.push(
        `${i + 1}. <code>${escapeHtml(d.fingerprint.slice(0, 16))}…</code> · آخر ظهور ${fmtDate(d.lastSeen)}`,
      );
    });
    if (record.devices.length > 50) lines.push(`… و${record.devices.length - 50} أخرى`);
    await tgSend(env, chatId, lines.join("\n"));
    return;
  }

  if (command === "reset") {
    const code = (args[0] || "").trim();
    const target = (args[1] || "").trim();
    const rec = code ? await kvGet(env, code) : null;
    if (!rec || !target) {
      await tgSend(env, chatId, "الصيغة: /reset &lt;code&gt; &lt;بصمة|all&gt;");
      return;
    }
    const record = upgradeRecord(code, rec);
    if (target === "all") {
      record.devices = [];
      await kvPut(env, code, record);
      await tgSend(env, chatId, `تم مسح كل أجهزة <code>${escapeHtml(code)}</code>.`);
      return;
    }
    const before = record.devices.length;
    record.devices = record.devices.filter((d) => d.fingerprint !== target && !d.fingerprint.startsWith(target));
    await kvPut(env, code, record);
    await tgSend(env, chatId, `تم حذف ${before - record.devices.length} جهاز.`);
    return;
  }

  await tgSend(env, chatId, "أمر غير معروف. /help");
}

async function handleTelegram(request, env, ctx) {
  const secret = env?.TELEGRAM_WEBHOOK_SECRET;
  if (secret) {
    const supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
    if (supplied !== secret) return new Response("forbidden", { status: 403 });
  }

  let update;
  try {
    update = await request.json();
  } catch {
    return json({ ok: true });
  }
  const message = update?.message || update?.edited_message;
  const text = message?.text;
  if (!message || !text) return json({ ok: true });

  const chatId = message.chat.id;
  const userId = message.from?.id;
  const admins = telegramAdmins(env);

  if (!admins.length) {
    await tgSend(
      env,
      chatId,
      `لم يتم ضبط المشرفين بعد. معرّفك: <code>${userId}</code>\n` +
        "أضِفه إلى السر TELEGRAM_ADMIN_IDS في الـWorker ثم أعد الإرسال.",
    );
    return json({ ok: true });
  }
  if (!admins.includes(userId)) {
    await tgSend(env, chatId, `غير مصرّح. معرّفك: <code>${userId}</code>`);
    return json({ ok: true });
  }

  const { command } = parseCommand(text);
  if (command === "id") {
    await tgSend(env, chatId, `معرّفك: <code>${userId}</code>`);
    return json({ ok: true });
  }

  try {
    await handleTelegramCommand(env, chatId, text);
  } catch (err) {
    await tgSend(env, chatId, `خطأ: ${escapeHtml(err?.message || err)}`);
  }
  return json({ ok: true });
}

// --------------------------------------------------------------------------
// Activation
// --------------------------------------------------------------------------

async function handleActivate(request, env, ctx) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return new Response("Server error", { status: 500 });
  }
  const { licenseCode, fingerprint } = payload || {};

  if (!(await allowedCodes(env)).includes(licenseCode)) {
    return new Response("Invalid license code", { status: 401 });
  }
  if (!fingerprint || typeof fingerprint !== "string" || fingerprint.trim().length === 0) {
    return new Response("Device fingerprint required", { status: 400 });
  }

  if (env?.LICENSES) {
    const now = Date.now();
    const raw = await kvGet(env, licenseCode);
    const record = upgradeRecord(licenseCode, raw);

    if (record.status === "revoked" || record.status === "disabled") {
      return new Response("License revoked", { status: 403 });
    }
    if (record.expires_at && now > record.expires_at) {
      return new Response("License expired", { status: 403 });
    }

    const index = record.devices.findIndex((d) => d.fingerprint === fingerprint);
    let isNewDevice = false;
    if (index === -1) {
      if (record.max_devices > 0 && record.devices.length >= record.max_devices) {
        return new Response("Maximum device limit reached", { status: 403 });
      }
      record.devices.push({ fingerprint, firstSeen: now, lastSeen: now });
      isNewDevice = true;
    } else {
      record.devices[index].lastSeen = now;
    }
    record.lastActivated = now;
    record.totalActivations += 1;
    record.activationAttempts += 1;
    await kvPut(env, licenseCode, record);

    if (isNewDevice && ctx?.waitUntil) {
      ctx.waitUntil(
        notifyAdmins(
          env,
          `جهاز جديد فُعّل\nالمفتاح: <code>${escapeHtml(licenseCode)}</code>\n` +
            `البصمة: <code>${escapeHtml(fingerprint.slice(0, 16))}…</code>\n` +
            `الأجهزة: ${record.devices.length}/${record.max_devices}`,
        ),
      );
    }
  }

  const expirationDate = Date.now() + YEAR_MS;
  const signature = await generateSignature(`${fingerprint}|${expirationDate}`, env);
  return json({ signature, expirationDate, durationHours: DURATION_HOURS });
}

// --------------------------------------------------------------------------
// Admin API (ADMIN_SECRET)
// --------------------------------------------------------------------------

function adminAuthorized(request, env) {
  const secret = env?.ADMIN_SECRET;
  if (!secret) return false;
  const supplied = request.headers.get("X-Admin-Secret") || "";
  return supplied.length === secret.length && supplied === secret;
}

async function handleAdminList(env) {
  const codes = await allCodes(env);
  const licenses = [];
  for (const code of codes) {
    const raw = await kvGet(env, code);
    const record = upgradeRecord(code, raw);
    licenses.push({
      code,
      migrated: !!raw && raw.schema_version === SCHEMA_VERSION,
      status: record.status,
      edition: record.edition,
      max_devices: record.max_devices,
      devices: record.devices.length,
      totalActivations: record.totalActivations,
      lastActivated: record.lastActivated,
      expires_at: record.expires_at,
    });
  }
  return json({ ok: true, schema_version: SCHEMA_VERSION, licenses });
}

async function handleAdminMigrate(request, env) {
  let body = {};
  try {
    body = (await request.json()) || {};
  } catch {
    body = {};
  }
  const requested = Array.isArray(body.codes) && body.codes.length ? body.codes : null;
  const codes = requested ?? (await allowedCodes(env));
  const migrated = [];
  for (const code of codes) {
    const raw = await kvGet(env, code);
    const record = upgradeRecord(code, raw);
    const ok = await kvPut(env, code, record);
    migrated.push({
      code,
      ok,
      schema_version: record.schema_version,
      devices: record.devices.length,
      max_devices: record.max_devices,
      totalActivations: record.totalActivations,
      created: raw == null,
    });
  }
  await writeIndex(env);
  return json({ ok: true, migrated });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/telegram/webhook") {
      if (request.method !== "POST") return new Response("Not found", { status: 404 });
      return handleTelegram(request, env, ctx);
    }

    if (url.pathname.startsWith("/admin/")) {
      if (!adminAuthorized(request, env)) {
        return new Response("Admin disabled or unauthorized", { status: 403 });
      }
      try {
        if (request.method === "GET" && url.pathname === "/admin/licenses") {
          return await handleAdminList(env);
        }
        if (request.method === "POST" && url.pathname === "/admin/migrate") {
          return await handleAdminMigrate(request, env);
        }
      } catch (err) {
        return json({ ok: false, error: String(err?.message || err) }, 500);
      }
      return new Response("Not found", { status: 404 });
    }

    if (request.method !== "POST" || url.pathname !== "/activate") {
      return new Response("Not found", { status: 404 });
    }

    try {
      return await handleActivate(request, env, ctx);
    } catch {
      return new Response("Server error", { status: 500 });
    }
  },
};
