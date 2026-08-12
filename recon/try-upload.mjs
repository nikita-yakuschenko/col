/**
 * Проверка идеи: загрузить файл в хранилище ДомКлик из контекста настоящей
 * страницы. Обычный HTTP-клиент получает 403 даже с верными куками, отпечатком
 * Chrome и всеми заголовками — запрос режет прокси перед приложением.
 *
 * Запуск: node try-upload.mjs
 */
import { chromium } from 'playwright';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const USER_DATA_DIR = path.join(HERE, '.userdata');
const CABINET = 'https://homeland-projects.domclick.ru/';

// Однопиксельный PNG.
const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGNgAAEAAAUAAQ0KLbQAAAAASUVORK5CYII=';

// Константа из публичного бандла ДомКлик, одинаковая во всех сессиях.
const ACCESS_TOKEN = process.env.DOMCLICK_ACCESS_TOKEN ?? '';

const upload = async (headless) => {
  const context = await chromium.launchPersistentContext(USER_DATA_DIR, { headless });
  const page = await context.newPage();
  await page.goto(CABINET, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(2500);

  const result = await page.evaluate(
    async ({ base64, token }) => {
      const binary = atob(base64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);

      const attempt = async (headers) => {
        const form = new FormData();
        form.append('file', new File([bytes], 'probe.png', { type: 'image/png' }));
        const reqId = 'chat-' + Math.random().toString(16).slice(2, 12);
        const url = `https://api.domclick.ru/storage/files?req-id=${reqId}&countDayStorage=1`;
        const response = await fetch(url, {
          method: 'POST',
          body: form,
          credentials: 'include',
          headers,
        });
        return { status: response.status, body: (await response.text()).slice(0, 200) };
      };

      return {
        withoutToken: await attempt({}),
        withToken: token ? await attempt({ 'x-access-token': token }) : 'токен не задан',
      };
    },
    { base64: PNG_BASE64, token: ACCESS_TOKEN },
  );

  await context.close();
  return result;
};

console.log('headless:', JSON.stringify(await upload(true), null, 2));
console.log('с окном: ', JSON.stringify(await upload(false), null, 2));
