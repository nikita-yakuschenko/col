/**
 * Разбор дампа, снятого capture.mjs.
 *
 * Сводит сырые NDJSON к трём вещам, которые нам нужны от разведки:
 *   - какой транспорт у чата (REST / WebSocket / SSE);
 *   - какие эндпоинты отдают сообщения и историю;
 *   - есть ли у клиента стабильный идентификатор, кроме ФИО.
 *
 * Запуск:  npm run analyze            (последняя сессия)
 *          npm run analyze -- <путь>  (конкретная сессия)
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_ROOT = path.resolve(HERE, '..', 'recon-out');

/** Слова, по которым узнаём «чатовые» эндпоинты и поля. */
const CHAT_HINTS = /chat|message|msg|dialog|conversation|thread|unread|feed|inbox|contractor|podryad/i;
const ID_HINTS = /^(?:.*_)?(?:id|uuid|guid)$/i;
const PII_HINTS = /phone|email|name|fio|client|user|customer|author|sender/i;

/**
 * Плавающий виджет чата часто оказывается сторонним сервисом, а не самописью.
 * Если увидим здесь чужой домен — у него, скорее всего, есть нормальный API.
 */
const KNOWN_VENDORS = {
  webim: /webim/i,
  jivo: /jivo(site)?/i,
  chat2desk: /chat2desk/i,
  'carrot quest': /carrotquest/i,
  bitrix24: /bitrix24|bitrix/i,
  livetex: /livetex/i,
  talkme: /talk-me|talkme/i,
  usedesk: /usedesk/i,
  intercom: /intercom/i,
  centrifugo: /centrifug/i,
  edna: /edna\./i,
};

const sessionDir = process.argv[2] ?? latestSession();
if (!sessionDir) {
  console.error('Дампов не найдено. Сначала: npm run capture');
  process.exit(1);
}
console.log(`Сессия: ${sessionDir}\n`);

const httpRecords = readNdjson(path.join(sessionDir, 'http.ndjson'));
const wsRecords = readNdjson(path.join(sessionDir, 'ws.ndjson'));

reportHosts();
reportTransport();
reportEndpoints();
reportChatPayloads();
reportIdentifiers();

function reportHosts() {
  const hosts = new Map();
  const bump = (url) => {
    try {
      const host = new URL(url).host;
      hosts.set(host, (hosts.get(host) ?? 0) + 1);
    } catch {
      /* мусорный url — пропускаем */
    }
  };
  httpRecords.forEach((r) => r.url && bump(r.url));
  wsRecords.forEach((r) => r.url && bump(r.url));

  console.log('=== Хосты в дампе ===');
  for (const [host, count] of [...hosts].sort((a, b) => b[1] - a[1])) {
    const vendor = Object.entries(KNOWN_VENDORS).find(([, re]) => re.test(host))?.[0];
    console.log(`  x${String(count).padEnd(5)} ${host}${vendor ? `   <-- похоже на ${vendor}` : ''}`);
  }
  console.log();
}

function reportTransport() {
  const sockets = new Set(wsRecords.filter((r) => r.kind === 'ws-open').map((r) => r.url));
  const sse = httpRecords.filter((r) => /event-stream/i.test(r.responseHeaders?.['content-type'] ?? ''));

  console.log('=== Транспорт ===');
  console.log(`WebSocket-соединений: ${sockets.size}`);
  sockets.forEach((url) => console.log(`  ${url}`));
  console.log(`WS-кадров: ${wsRecords.filter((r) => r.kind?.startsWith('ws-s') || r.kind === 'ws-recv').length}`);
  console.log(`SSE-потоков: ${sse.length}`);
  sse.forEach((r) => console.log(`  ${r.url}`));
  console.log();
}

function reportEndpoints() {
  const groups = new Map();
  for (const record of httpRecords) {
    if (record.kind !== 'http') continue;
    const key = `${record.method} ${normalize(record.url)}`;
    const group = groups.get(key) ?? { key, count: 0, statuses: new Set(), sample: record };
    group.count += 1;
    group.statuses.add(record.status);
    groups.set(key, group);
  }

  const chatty = [...groups.values()].filter((g) => CHAT_HINTS.test(g.key));
  console.log(`=== Эндпоинты, похожие на чат (${chatty.length} из ${groups.size}) ===`);
  for (const group of chatty.sort((a, b) => b.count - a.count)) {
    console.log(`  [${[...group.statuses].join(',')}] x${group.count}  ${group.key}`);
  }
  console.log();
}

function reportChatPayloads() {
  console.log('=== Примеры полезной нагрузки ===');
  const shown = new Set();

  for (const record of httpRecords) {
    if (!record.body || !CHAT_HINTS.test(record.url)) continue;
    const key = normalize(record.url);
    if (shown.has(key)) continue;
    shown.add(key);
    console.log(`\n--- ${record.method} ${key}`);
    console.log(preview(record.body));
  }

  const wsShapes = new Map();
  for (const record of wsRecords) {
    if (!record.payload) continue;
    const parsed = tryJson(record.payload);
    const shape = parsed ? Object.keys(parsed).sort().join(',') : `<raw:${record.payload.slice(0, 24)}>`;
    if (wsShapes.has(shape)) continue;
    wsShapes.set(shape, record);
  }
  for (const [shape, record] of wsShapes) {
    console.log(`\n--- ${record.kind} [${shape}]`);
    console.log(preview(record.payload));
  }
  console.log();
}

function reportIdentifiers() {
  const fields = new Map();
  const visit = (node, trail) => {
    if (node === null || typeof node !== 'object') return;
    if (Array.isArray(node)) return node.slice(0, 3).forEach((item) => visit(item, trail));
    for (const [key, value] of Object.entries(node)) {
      const pathKey = trail ? `${trail}.${key}` : key;
      if ((ID_HINTS.test(key) || PII_HINTS.test(key)) && typeof value !== 'object') {
        const bucket = fields.get(pathKey) ?? new Set();
        if (bucket.size < 3) bucket.add(String(value).slice(0, 60));
        fields.set(pathKey, bucket);
      }
      visit(value, pathKey);
    }
  };

  for (const record of httpRecords) {
    if (record.body && CHAT_HINTS.test(record.url)) visit(tryJson(record.body), '');
  }
  for (const record of wsRecords) {
    if (record.payload) visit(tryJson(record.payload), '');
  }

  console.log('=== Кандидаты в идентификаторы и контакты клиента ===');
  console.log('(ищем стабильный ключ для user.id в imconnector.send.messages)');
  for (const [field, values] of [...fields].sort()) {
    console.log(`  ${field} = ${[...values].join(' | ')}`);
  }
  console.log();
}

// --- вспомогательное -------------------------------------------------------

function latestSession() {
  if (!fs.existsSync(OUT_ROOT)) return null;
  const dirs = fs
    .readdirSync(OUT_ROOT, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(OUT_ROOT, entry.name))
    .sort();
  return dirs.at(-1) ?? null;
}

function readNdjson(file) {
  if (!fs.existsSync(file)) return [];
  return fs
    .readFileSync(file, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => tryJson(line))
    .filter(Boolean);
}

function tryJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** Схлопывает числовые и uuid-сегменты пути, чтобы группировать однотипные вызовы. */
function normalize(url) {
  const { origin, pathname } = new URL(url);
  const masked = pathname
    .replace(/\/\d+(?=\/|$)/g, '/{id}')
    .replace(/\/[0-9a-f]{8}-[0-9a-f-]{27}(?=\/|$)/gi, '/{uuid}');
  return origin + masked;
}

function preview(text) {
  const parsed = tryJson(text);
  const pretty = parsed ? JSON.stringify(parsed, null, 2) : text;
  return pretty.length > 1500 ? pretty.slice(0, 1500) + '\n… (обрезано)' : pretty;
}
