// Test-only, disposable PostgreSQL over stdio. No network listener or credentials.
const { PGlite } = require(process.env.COGNITIVE_TEST_PGLITE);
const readline = require('node:readline');
(async () => {
  const db = new PGlite();
  await db.waitReady;
  for await (const line of readline.createInterface({ input: process.stdin })) {
    try {
      const { sql, params } = JSON.parse(line);
      const result = await db.query(sql, params);
      process.stdout.write(JSON.stringify(result) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({ error: String(error) }) + '\n');
    }
  }
  await db.close();
})();
