/**
 * Выгружает куки из постоянного профиля разведки в storage-state.json.
 *
 * Нужно, чтобы боевой клиент мог ходить в ДомКлик без браузера: берём готовую
 * сессию, которую человек один раз подтвердил по SMS, и переиспользуем её.
 *
 * Запуск: npm run export-cookies
 */
import { chromium } from 'playwright';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const USER_DATA_DIR = path.join(HERE, '.userdata');
const OUT = path.resolve(HERE, '..', 'storage-state.json');

const CABINET = process.env.DOMCLICK_START_URL ?? 'https://homeland-projects.domclick.ru/';

const context = await chromium.launchPersistentContext(USER_DATA_DIR, { headless: true });

// Обязательно открыть страницу: без визита Playwright не собирает localStorage,
// а там лежит x-access-token, без которого не работает загрузка файлов.
const page = await context.newPage();
await page.goto(CABINET, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
await page.waitForTimeout(3000);

const state = await context.storageState({ path: OUT });
await context.close();

const domains = new Map();
for (const cookie of state.cookies) {
  domains.set(cookie.domain, (domains.get(cookie.domain) ?? 0) + 1);
}

console.log(`Кук выгружено: ${state.cookies.length} -> ${OUT}`);
for (const [domain, count] of [...domains].sort((a, b) => b[1] - a[1]).slice(0, 10)) {
  console.log(`  x${count}\t${domain}`);
}

const origins = state.origins ?? [];
console.log(`Origins с localStorage: ${origins.length}`);
for (const origin of origins) {
  console.log(`  ${origin.origin}: ${(origin.localStorage ?? []).length} ключей`);
}
