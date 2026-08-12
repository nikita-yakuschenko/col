/**
 * Разведка сетевого обмена личного кабинета ДомКлик.
 *
 * Открывает видимый браузер с постоянным профилем и пишет в NDJSON всё,
 * что уходит и приходит по сети: HTTP-запросы с телами ответов и кадры
 * WebSocket. Логинимся и кликаем руками — задача скрипта только записывать.
 *
 * Запуск:  npm run capture
 * Остановка: закрыть окно браузера или Ctrl+C.
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

loadDotEnv(path.join(ROOT, '.env'));

// Чат живёт только в кабинете «Построить дом»: на основном домклике его нет.
const START_URL = process.env.DOMCLICK_START_URL ?? 'https://homeland-projects.domclick.ru/';
const USER_DATA_DIR = path.join(HERE, '.userdata');
const SESSION_DIR = path.join(ROOT, 'recon-out', new Date().toISOString().replace(/[:.]/g, '-'));

/** Ресурсы, которые только шумят в дампе. */
const SKIP_TYPES = new Set(['image', 'font', 'stylesheet', 'media']);

/** Картинки обычно шум, но при разборе вложений именно они и нужны: `--images`. */
const KEEP_IMAGES = process.argv.includes('--images');

/** Аналитика и трекеры — не наша цель. */
const SKIP_HOSTS = /(?:mc\.yandex|google-analytics|googletagmanager|doubleclick|criteo|top-fwz1|top-mail|vk\.com\/rtrg|sentry\.io|smartlook|hotjar|facebook\.net)/i;

/** Тела ответов снимаем только у запросов данных и только текстовые. */
const BODY_TYPES = new Set(['xhr', 'fetch', 'document']);
const MAX_BODY = 512 * 1024;

/** Селекторы формы входа. Объявлены до вызова autoLogin — const не всплывает. */
const PHONE_SELECTORS = [
  'input[type="tel"]',
  'input[autocomplete="tel"]',
  'input[name*="phone" i]',
  'input[placeholder*="телефон" i]',
];
const PASSWORD_SELECTORS = ['input[type="password"]', 'input[name*="pass" i]'];
const SUBMIT_SELECTORS = [
  'button[type="submit"]',
  'button:has-text("Войти")',
  'button:has-text("Продолжить")',
  'button:has-text("Далее")',
];

fs.mkdirSync(SESSION_DIR, { recursive: true });
const http = fs.createWriteStream(path.join(SESSION_DIR, 'http.ndjson'), { flags: 'a' });
const ws = fs.createWriteStream(path.join(SESSION_DIR, 'ws.ndjson'), { flags: 'a' });

let httpCount = 0;
let wsCount = 0;

const write = (stream, record) => {
  stream.write(JSON.stringify({ ts: Date.now(), ...record }) + '\n');
};

const interesting = (url, resourceType) => {
  if (SKIP_HOSTS.test(url)) return false;
  if (KEEP_IMAGES && resourceType === 'image') return true;
  return !SKIP_TYPES.has(resourceType);
};

const context = await chromium.launchPersistentContext(USER_DATA_DIR, {
  headless: false,
  viewport: null,
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
  args: ['--start-maximized'],
});

context.on('response', async (response) => {
  const request = response.request();
  const url = request.url();
  const resourceType = request.resourceType();
  if (!interesting(url, resourceType)) return;

  const record = {
    kind: 'http',
    method: request.method(),
    url,
    resourceType,
    status: response.status(),
    requestHeaders: await request.allHeaders().catch(() => ({})),
    responseHeaders: await response.allHeaders().catch(() => ({})),
    postData: request.postData() ?? null,
  };

  // Бинарные тела (multipart с файлом) в postData не попадают, а именно они и
  // нужны, чтобы увидеть имена полей формы. Кладём в base64, с ограничением.
  if (!record.postData && request.method() !== 'GET') {
    const buffer = request.postDataBuffer();
    if (buffer && buffer.length <= 1024 * 1024) {
      record.postDataBase64 = buffer.toString('base64');
      record.postDataBytes = buffer.length;
    } else if (buffer) {
      record.postDataBytes = buffer.length;
    }
  }

  // Тело может быть уже недоступно (редирект, отменённый запрос) — это нормально.
  if (BODY_TYPES.has(resourceType)) {
    try {
      const contentType = record.responseHeaders['content-type'] ?? '';
      if (/json|text|javascript/i.test(contentType)) {
        const buffer = await response.body();
        record.body =
          buffer.length > MAX_BODY
            ? `<<${buffer.length} bytes, truncated>>${buffer.subarray(0, MAX_BODY).toString('utf8')}`
            : buffer.toString('utf8');
      }
    } catch {
      record.body = null;
    }
  }

  write(http, record);
  httpCount += 1;
});

/** WebSocket живёт на странице, а не на контексте, — вешаемся на каждую вкладку. */
const watchPage = (page) => {
  page.on('websocket', (socket) => {
    write(ws, { kind: 'ws-open', url: socket.url() });
    socket.on('framesent', ({ payload }) => {
      write(ws, { kind: 'ws-sent', url: socket.url(), payload: String(payload) });
      wsCount += 1;
    });
    socket.on('framereceived', ({ payload }) => {
      write(ws, { kind: 'ws-recv', url: socket.url(), payload: String(payload) });
      wsCount += 1;
    });
    socket.on('close', () => write(ws, { kind: 'ws-close', url: socket.url() }));
  });

  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) write(http, { kind: 'navigation', url: frame.url() });
  });
};

context.on('page', watchPage);
context.pages().forEach(watchPage);

const page = context.pages()[0] ?? (await context.newPage());
await page.goto(START_URL, { waitUntil: 'domcontentloaded' }).catch(() => {});

if (process.env.DOMCLICK_AUTOLOGIN !== '0') {
  await autoLogin(page);
  await openChat(page);
}

console.log(`
  Пишу трафик в ${SESSION_DIR}

  Если автологин не справился — доделай руками, запись всё равно идёт.
  Осталось прокликать в чате (это нужно, чтобы увидеть недостающие методы):
    - открыть переписку с клиентом;
    - проскроллить историю вверх — всплывёт подгрузка старых сообщений;
    - отправить тестовое сообщение — увидим формат отправки;
    - закрыть браузер.
`);

// Куки снимаем по таймеру: в обработчике close контекст уже мёртв и storageState падает.
const STATE_PATH = path.join(SESSION_DIR, 'storage-state.json');
const snapshotState = () => context.storageState({ path: STATE_PATH }).catch(() => {});
const stateTimer = setInterval(snapshotState, 20000);
await snapshotState();

let finished = false;
const finish = async () => {
  if (finished) return;
  finished = true;
  clearInterval(stateTimer);
  await new Promise((resolve) => http.end(resolve));
  await new Promise((resolve) => ws.end(resolve));
  console.log(`Готово: ${httpCount} HTTP-обменов, ${wsCount} WS-кадров -> ${SESSION_DIR}`);
  process.exit(0);
};

context.on('close', finish);
process.on('SIGINT', finish);

// --- автоматизация входа ---------------------------------------------------
//
// Селекторы подобраны вслепую, вёрстку кабинета мы ещё не видели, поэтому всё
// построено на переборе и любой шаг может не найтись. Это не страшно: запись
// трафика идёт независимо, недостающее всегда можно дожать руками.

/** Ищет первый видимый элемент по списку селекторов — в самой странице и во всех её фреймах. */
async function firstVisible(page, selectors, timeout = 4000) {
  for (const root of [page, ...page.frames()]) {
    for (const selector of selectors) {
      const locator = root.locator(selector).first();
      try {
        await locator.waitFor({ state: 'visible', timeout });
        return locator;
      } catch {
        /* следующий кандидат */
      }
    }
    timeout = 500; // полный таймаут тратим только на первый заход
  }
  return null;
}

async function autoLogin(page) {
  const password = process.env.DOMCLICK_PASSWORD;
  const phones = [
    process.env.DOMCLICK_USER_1,
    process.env.DOMCLICK_USER_2,
    process.env.DOMCLICK_USER_3,
  ].filter(Boolean);

  if (!password || phones.length === 0) {
    console.log('Автологин пропущен: в .env нет телефона или пароля.');
    return false;
  }

  // Профиль постоянный — со второго запуска формы входа может уже не быть.
  if (!(await firstVisible(page, PHONE_SELECTORS, 6000))) {
    console.log('Форма входа не найдена — похоже, сессия из профиля ещё жива.');
    return true;
  }

  for (const phone of phones) {
    console.log(`Пробую войти с телефоном в формате ${phone.slice(0, 2)}…`);
    const phoneField = await firstVisible(page, PHONE_SELECTORS, 3000);
    if (!phoneField) break;

    await phoneField.click();
    // Плейсхолдер-маска ведёт себя странно, поэтому чистим поле и печатаем посимвольно.
    await phoneField.fill('').catch(() => {});
    await phoneField.pressSequentially(phone, { delay: 60 });

    const passwordField = await firstVisible(page, PASSWORD_SELECTORS, 2000);
    if (passwordField) await passwordField.fill(password);

    const submit = await firstVisible(page, SUBMIT_SELECTORS, 2000);
    if (submit) await submit.click().catch(() => {});
    else await phoneField.press('Enter').catch(() => {});

    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});

    // Пароль мог появиться только на втором шаге — доводим вход.
    const latePassword = await firstVisible(page, PASSWORD_SELECTORS, 3000);
    if (latePassword) {
      await latePassword.fill(password);
      const nextSubmit = await firstVisible(page, SUBMIT_SELECTORS, 2000);
      if (nextSubmit) await nextSubmit.click().catch(() => {});
      else await latePassword.press('Enter').catch(() => {});
      await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    }

    if (!(await firstVisible(page, PHONE_SELECTORS, 3000))) {
      console.log('Вход выполнен.');
      return true;
    }
    console.log('Этот формат телефона не подошёл, пробую следующий.');
  }

  console.log('Автологин не удался — залогинься руками, запись продолжается.');
  return false;
}

/** Плавающая кнопка чата в правом нижнем углу: подгружается лениво, ждём подольше. */
async function openChat(page) {
  await page.waitForTimeout(4000);
  const button = await firstVisible(
    page,
    [
      '[class*="chat" i] button',
      'button[aria-label*="чат" i]',
      'button[aria-label*="chat" i]',
      '[data-test*="chat" i]',
      '[id*="chat" i] button',
      'iframe[src*="chat" i]',
    ],
    8000,
  );

  if (!button) {
    console.log('Иконку чата автоматически не нашёл — открой её сам, в правом нижнем углу.');
    return false;
  }
  await button.click().catch(() => {});
  await page.waitForTimeout(3000);
  console.log('Чат открыт.');
  return true;
}

/** Минимальный парсер .env, чтобы не тянуть зависимость. */
function loadDotEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const match = /^\s*([\w.-]+)\s*=\s*(.*)?\s*$/.exec(line);
    if (!match) continue;
    const value = (match[2] ?? '').trim().replace(/^(['"])(.*)\1$/, '$2');
    if (!(match[1] in process.env)) process.env[match[1]] = value;
  }
}
