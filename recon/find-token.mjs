/**
 * Ищет токен для хранилища файлов в localStorage разных доменов ДомКлик.
 * Значение не печатает — кладёт в файл рядом, чтобы им могли пользоваться скрипты.
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const USER_DATA_DIR = path.join(HERE, '.userdata');
const OUT = path.resolve(HERE, '..', 'domclick-token.txt');

const ORIGINS = [
  'https://domclick.ru/',
  'https://homeland-projects.domclick.ru/',
];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const context = await chromium.launchPersistentContext(USER_DATA_DIR, { headless: true });
const page = await context.newPage();

let found = '';
for (const origin of ORIGINS) {
  await page.goto(origin, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(2500);

  const entries = await page.evaluate(() =>
    Object.entries(localStorage).map(([key, value]) => [key, String(value)]),
  );

  console.log(`\n${page.url()} — ключей: ${entries.length}`);
  for (const [key, value] of entries) {
    const candidate = value.split(':')[0].replace(/^"|"$/g, '');
    const isToken = UUID.test(candidate);
    console.log(`   ${key}: длина ${value.length}${isToken ? '   <-- UUID, похоже на токен' : ''}`);
    if (isToken && !found) found = candidate;
  }
}

if (found) {
  fs.writeFileSync(OUT, found, 'utf8');
  console.log(`\nтокен сохранён в ${OUT} (длина ${found.length})`);
} else {
  console.log('\nтокен не найден');
}

await context.close();
