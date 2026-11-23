// Nano / Cashir activation Worker — license schema v2 + Telegram admin bot.
//
// Endpoints:
//   POST /activate            registers a device and returns a signed payload
//                             { payload, signature, edition, expiresAt, ... }
//   POST /revalidate          same answer for an already-registered device but
//                             never registers a new one or bumps the counters
//   POST /telegram/webhook    Telegram admin bot (see commands below)
//   GET  /admin/licenses      listing (guarded by ADMIN_SECRET)
//   POST /admin/migrate       upgrade stored records to schema v2 (guarded)
//
// Telegram admin commands (only ids in TELEGRAM_ADMIN_IDS may run them):
//   /menu | /start              inline-button menu (list/stats/add/help)
//   /help                       show this help
//   /id                         show your Telegram id (bootstrap)
//   /list                       list every license
//   /stats                      totals
//   /add <code> [devices] [days]
//   /edition <code> <name>      set the license edition (standard/pro/...)
//   /del <code>                 delete a license
//   /disable <code> | /enable <code>
//   /extend <code> <days>
//   /devices <code>             list registered devices
//   /reset <code> <fingerprint|all>
// Key cards (inline buttons) do: enable/disable, extend 30d, devices,
// edition, delete (with confirmation) -- driven by callback_query updates.
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
  // URL-safe base64 without padding so the Python client can verify with the
  // exact same canonical spelling it stores.
  const bytes = new Uint8Array(signature);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
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

// Drop a code from the allow-list index (used by /del). writeIndex() recomputes
// from allCodes(), which still reads the old index — so it can never remove an
// entry on its own.
async function removeFromIndex(env, code) {
  const index = await kvGet(env, INDEX_KEY);
  const set = new Set(Array.isArray(index) ? index : []);
  set.delete(code);
  set.delete(INDEX_KEY);
  const codes = [...set].sort();
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

function tgEdit(env, chatId, messageId, text, keyboard) {
  const payload = {
    chat_id: chatId,
    message_id: messageId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  };
  payload.reply_markup = keyboard ? { inline_keyboard: keyboard } : { inline_keyboard: [] };
  return tgCall(env, "editMessageText", payload);
}

function tgAnswer(env, callbackId, text) {
  const payload = { callback_query_id: callbackId };
  if (text) payload.text = text;
  return tgCall(env, "answerCallbackQuery", payload);
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
  "/menu — القائمة بالأزرار",
  "/list — كل المفاتيح",
  "/stats — ملخص",
  "/add &lt;code&gt; [أجهزة] [أيام] — إنشاء مفتاح",
  "/edition &lt;code&gt; &lt;standard|pro|...&gt; — ضبط الإصدار",
  "/del &lt;code&gt; [force] — حذف",
  "/disable &lt;code&gt; / /enable &lt;code&gt;",
  "/extend &lt;code&gt; &lt;أيام&gt;",
  "/devices &lt;code&gt; — الأجهزة",
  "/reset &lt;code&gt; &lt;بصمة|all&gt;",
  "/id — معرّفك",
].join("\n");

const EDITION_PRESETS = ["standard", "pro", "premium"];
const BACK_ROW = [{ text: "⬅️ القائمة", callback_data: "menu" }];

function keyStatusIcon(rec) {
  return rec.status === "active" ? "✅" : "⛔️";
}

function backToKey(code) {
  return [{ text: "⬅️ رجوع", callback_data: `k|${encodeURIComponent(code)}|view` }];
}

function menuView() {
  return {
    text: "<b>🛠 لوحة تفعيل كاشير</b>\nاختر إجراءً:",
    keyboard: [
      [{ text: "📋 المفاتيح", callback_data: "menu|list" }],
      [
        { text: "📊 ملخص", callback_data: "menu|stats" },
        { text: "➕ إضافة مفتاح", callback_data: "menu|add" },
      ],
      [{ text: "❓ مساعدة", callback_data: "menu|help" }],
    ],
  };
}

function addHelpText() {
  return (
    "<b>➕ إضافة مفتاح</b>\nأرسل:\n" +
    "<code>/add اسم_المفتاح [أجهزة] [أيام]</code>\n\n" +
    "مثال: <code>/add hawa 330 0</code>\n" +
    "أيام=0 تعني ترخيصًا دائمًا."
  );
}

function keyCard(code, rec) {
  const exp = rec.expires_at ? fmtDate(rec.expires_at) : "دائم/غير محدد";
  const lines = [
    `<b>🔑 ${escapeHtml(code)}</b>`,
    `الحالة: ${keyStatusIcon(rec)} ${rec.status === "active" ? "مفعّل" : "معطّل"}`,
    `الإصدار: <code>${escapeHtml(rec.edition || "standard")}</code>`,
    `الأجهزة: ${rec.devices.length}/${rec.max_devices}`,
    `الانتهاء: ${exp}`,
    `التفعيلات: ${rec.totalActivations}`,
  ];
  const toggle =
    rec.status === "active"
      ? { text: "⛔️ تعطيل", callback_data: `k|${encodeURIComponent(code)}|disable` }
      : { text: "✅ تفعيل", callback_data: `k|${encodeURIComponent(code)}|enable` };
  const keyboard = [
    [toggle, { text: "➕ تمديد ٣٠ يومًا", callback_data: `k|${encodeURIComponent(code)}|extend30` }],
    [
      { text: "🤖 الأجهزة", callback_data: `k|${encodeURIComponent(code)}|devices` },
      { text: "🏷 الإصدار", callback_data: `k|${encodeURIComponent(code)}|edition` },
    ],
    [
      { text: "🗑 حذف", callback_data: `k|${encodeURIComponent(code)}|del` },
      { text: "⬅️ القائمة", callback_data: "menu" },
    ],
  ];
  return { text: lines.join("\n"), keyboard };
}

function devicesCard(code, rec) {
  const lines = [`<b>🤖 أجهزة ${escapeHtml(code)}</b> (${rec.devices.length}/${rec.max_devices})`];
  if (!rec.devices.length) {
    lines.push("لا توجد أجهزة مسجّلة.");
  } else {
    rec.devices.slice(0, 50).forEach((d, i) => {
      lines.push(`${i + 1}. <code>${escapeHtml(d.fingerprint.slice(0, 16))}...</code> · آخر ظهور ${fmtDate(d.lastSeen)}`);
    });
    if (rec.devices.length > 50) lines.push(`... و${rec.devices.length - 50} أخرى`);
  }
  return { text: lines.join("\n"), keyboard: [backToKey(code)] };
}

function editionCard(code, rec) {
  const rows = EDITION_PRESETS.map((e) => [
    { text: (rec.edition === e ? "◉ " : "○ ") + e, callback_data: `k|${encodeURIComponent(code)}|setedition|${e}` },
  ]);
  rows.push(backToKey(code));
  return {
    text: `<b>🏷 إصدار ${escapeHtml(code)}</b>\nالحالي: <code>${escapeHtml(rec.edition || "standard")}</code>\nاختر إصدارًا:`,
    keyboard: rows,
  };
}

function deleteConfirm(code, rec) {
  return {
    text:
      `⚠️ <b>تأكيد الحذف</b>\nسيُحذف المفتاح <code>${escapeHtml(code)}</code> ` +
      `و${rec.devices.length} جهاز مرتبط به نهائيًا.`,
    keyboard: [
      [{ text: "🗑 نعم، احذف", callback_data: `k|${encodeURIComponent(code)}|delconfirm` }],
      [{ text: "↩️ إلغاء", callback_data: `k|${encodeURIComponent(code)}|view` }],
    ],
  };
}

async function listView(env) {
  const codes = await allCodes(env);
  if (!codes.length) {
    return { text: "لا توجد مفاتيح.", keyboard: [BACK_ROW] };
  }
  const rows = [];
  for (const code of codes) {
    const rec = upgradeRecord(code, await kvGet(env, code));
    rows.push([
      {
        text: `${keyStatusIcon(rec)} ${code} · ${rec.devices.length}/${rec.max_devices}`,
        callback_data: `k|${encodeURIComponent(code)}|view`,
      },
    ]);
  }
  rows.push(BACK_ROW);
  return { text: `<b>📋 المفاتيح</b> (${codes.length})`, keyboard: rows };
}

async function statsView(env) {
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
  return {
    text:
      `<b>📊 ملخص</b>\nالمفاتيح: ${codes.length} (فعّالة ${active})\n` +
      `الأجهزة: ${devices}\nالتفعيلات: ${activations}`,
    keyboard: [BACK_ROW],
  };
}

async function handleTelegramCommand(env, chatId, text) {
  const { command, args } = parseCommand(text);

  if (command === "help") {
    await tgSend(env, chatId, HELP_TEXT);
    return;
  }

  if (command === "menu" || command === "start") {
    const v = menuView();
    await tgSend(env, chatId, v.text, v.keyboard);
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
        `${status} <code>${escapeHtml(code)}</code> · ${escapeHtml(rec.edition || "standard")}` +
          ` — أجهزة ${rec.devices.length}/${rec.max_devices}` +
          ` · تفعيلات ${rec.totalActivations}${exp}`,
      );
    }
    const v = await listView(env);
    await tgSend(env, chatId, lines.join("\n"), v.keyboard);
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
    const force = args.includes("force");
    if (!code) {
      await tgSend(env, chatId, "الصيغة: /del &lt;code&gt; [force]");
      return;
    }
    const record = upgradeRecord(code, await kvGet(env, code));
    if (record.devices.length && !force) {
      await tgSend(
        env,
        chatId,
        `المفتاح <code>${escapeHtml(code)}</code> مرتبط بـ ${record.devices.length} جهاز.\n` +
          `استخدم /disable لإيقافه، أو <code>/del ${escapeHtml(code)} force</code> للحذف النهائي.`,
      );
      return;
    }
    await kvDelete(env, code);
    await removeFromIndex(env, code);
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

  if (command === "edition") {
    const code = (args[0] || "").trim();
    const value = (args[1] || "").trim();
    const rec = code ? await kvGet(env, code) : null;
    if (!rec || !/^[A-Za-z0-9_.-]{1,32}$/.test(value)) {
      await tgSend(env, chatId, "الصيغة: /edition &lt;code&gt; &lt;standard|pro|...&gt;");
      return;
    }
    const record = upgradeRecord(code, rec);
    record.edition = value;
    await kvPut(env, code, record);
    await tgSend(
      env,
      chatId,
      `تم ضبط إصدار <code>${escapeHtml(code)}</code> إلى <code>${escapeHtml(value)}</code>.`,
    );
    return;
  }

  await tgSend(env, chatId, "أمر غير معروف. /help");
}

async function handleCallback(env, cb) {
  const chatId = cb.message.chat.id;
  const messageId = cb.message.message_id;
  const parts = String(cb.data || "").split("|");
  const kind = parts[0];

  if (kind === "menu") {
    const action = parts[1] || "root";
    if (action === "list") {
      const v = await listView(env);
      await tgEdit(env, chatId, messageId, v.text, v.keyboard);
    } else if (action === "stats") {
      const v = await statsView(env);
      await tgEdit(env, chatId, messageId, v.text, v.keyboard);
    } else if (action === "add") {
      await tgEdit(env, chatId, messageId, addHelpText(), [BACK_ROW]);
    } else if (action === "help") {
      await tgEdit(env, chatId, messageId, HELP_TEXT, [BACK_ROW]);
    } else {
      const v = menuView();
      await tgEdit(env, chatId, messageId, v.text, v.keyboard);
    }
    return;
  }

  if (kind !== "k") {
    await tgEdit(env, chatId, messageId, "إجراء غير معروف.", [BACK_ROW]);
    return;
  }

  const code = decodeURIComponent(parts[1] || "");
  const action = parts[2] || "view";
  const raw = code ? await kvGet(env, code) : null;
  if (!raw) {
    await tgEdit(env, chatId, messageId, `المفتاح <code>${escapeHtml(code)}</code> غير موجود.`, [BACK_ROW]);
    return;
  }
  const rec = upgradeRecord(code, raw);

  if (action === "disable" || action === "enable") {
    rec.status = action === "enable" ? "active" : "disabled";
    await kvPut(env, code, rec);
  } else if (action === "extend30") {
    rec.expires_at = Math.max(Date.now(), rec.expires_at || 0) + 30 * 86400000;
    await kvPut(env, code, rec);
  } else if (action === "setedition") {
    const value = (parts[3] || "").trim();
    if (/^[A-Za-z0-9_.-]{1,32}$/.test(value)) {
      rec.edition = value;
      await kvPut(env, code, rec);
    }
    const v = editionCard(code, rec);
    await tgEdit(env, chatId, messageId, v.text, v.keyboard);
    return;
  } else if (action === "edition") {
    const v = editionCard(code, rec);
    await tgEdit(env, chatId, messageId, v.text, v.keyboard);
    return;
  } else if (action === "devices") {
    const v = devicesCard(code, rec);
    await tgEdit(env, chatId, messageId, v.text, v.keyboard);
    return;
  } else if (action === "del") {
    const v = deleteConfirm(code, rec);
    await tgEdit(env, chatId, messageId, v.text, v.keyboard);
    return;
  } else if (action === "delconfirm") {
    await kvDelete(env, code);
    await removeFromIndex(env, code);
    const v = await listView(env);
    await tgEdit(env, chatId, messageId, `🗑 تم حذف <code>${escapeHtml(code)}</code>.\n\n${v.text}`, v.keyboard);
    return;
  }

  const v = keyCard(code, rec);
  await tgEdit(env, chatId, messageId, v.text, v.keyboard);
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

  const admins = telegramAdmins(env);

  const cb = update?.callback_query;
  if (cb) {
    const chatId = cb.message?.chat?.id;
    const userId = cb.from?.id;
    await tgAnswer(env, cb.id);
    if (!admins.length || !admins.includes(userId)) {
      return json({ ok: true });
    }
    try {
      await handleCallback(env, cb);
    } catch (err) {
      if (chatId) await tgSend(env, chatId, `خطأ: ${escapeHtml(err?.message || err)}`);
    }
    return json({ ok: true });
  }

  const message = update?.message || update?.edited_message;
  const text = message?.text;
  if (!message || !text) return json({ ok: true });

  const chatId = message.chat.id;
  const userId = message.from?.id;

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

async function handleActivation(request, env, ctx, { count }) {
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

  const now = Date.now();
  let record = null;

  if (env?.LICENSES) {
    const raw = await kvGet(env, licenseCode);
    record = upgradeRecord(licenseCode, raw);

    if (record.status === "revoked" || record.status === "disabled") {
      return new Response("License revoked", { status: 403 });
    }
    if (record.expires_at && now > record.expires_at) {
      return new Response("License expired", { status: 403 });
    }

    const index = record.devices.findIndex((d) => d.fingerprint === fingerprint);
    let isNewDevice = false;
    if (index === -1) {
      // A heartbeat never registers a device: if admin reset/removed it, the
      // device must re-activate explicitly with the key.
      if (!count) {
        return new Response("Device not registered", { status: 403 });
      }
      if (record.max_devices > 0 && record.devices.length >= record.max_devices) {
        return new Response("Maximum device limit reached", { status: 403 });
      }
      record.devices.push({ fingerprint, firstSeen: now, lastSeen: now });
      isNewDevice = true;
    } else {
      record.devices[index].lastSeen = now;
    }
    record.lastActivated = now;
    if (count) {
      record.totalActivations += 1;
      record.activationAttempts += 1;
    }
    await kvPut(env, licenseCode, record);

    if (isNewDevice && count && ctx?.waitUntil) {
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

  // The record (not a hard-coded window) is the source of truth for the
  // license edition and expiry, so the app shows exactly what admin set in the
  // Telegram bot (/add days, /extend, /disable). `expiresAt` is authoritative
  // and null means "lifetime"; `expirationDate` is kept as the legacy numeric
  // field other clients already rely on.
  const expiresAt = record && Number.isFinite(record.expires_at) ? record.expires_at : null;
  const expirationDate = expiresAt || now + YEAR_MS;
  const edition = (record && record.edition) || "standard";
  const status = (record && record.status) || "active";
  // The signed payload is the cryptographic proof the app verifies offline.
  // The client rejects any response not signed by SIGNING_KEY, so a license
  // can no longer be forged by editing local files or a fake server.
  const signedPayload = JSON.stringify({
    v: 2,
    code: licenseCode,
    fingerprint,
    edition,
    expiresAt,
    status,
    issuedAt: now,
  });
  const signature = await generateSignature(signedPayload, env);
  return json({
    success: true,
    payload: signedPayload,
    signature,
    edition,
    expiresAt,
    expirationDate,
    durationHours: DURATION_HOURS,
    status,
    maxDevices: record ? record.max_devices : DEFAULT_MAX_DEVICES,
    devices: record ? record.devices.length : 0,
  });
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

    if (request.method === "POST" && url.pathname === "/activate") {
      try {
        return await handleActivation(request, env, ctx, { count: true });
      } catch {
        return new Response("Server error", { status: 500 });
      }
    }

    if (request.method === "POST" && url.pathname === "/revalidate") {
      try {
        return await handleActivation(request, env, ctx, { count: false });
      } catch {
        return new Response("Server error", { status: 500 });
      }
    }

    return new Response("Not found", { status: 404 });
  },
};
