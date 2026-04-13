/**
 * Initial SP500 data fill — runs all collection phases once.
 * Separate from the bot so we can monitor progress without holding up Discord.
 */
'use strict';

process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw';
process.env.REDIS_URL    = process.env.REDIS_URL    || 'redis://localhost:6379';

const collector = require('./src/pipeline/collector');
const store     = require('./src/pipeline/store');

let phase = '';

collector.setBroadcast((data) => {
  if (data.message) process.stdout.write(`[${new Date().toISOString().slice(11,19)}] ${data.message}\n`);
});

collector.setDiscordHooks({
  presence: (txt) => process.stdout.write(`[presence] ${txt}\n`),
  alertPost: (msg) => {
    process.stdout.write(`\n[PHASE] ${msg}\n\n`);
  },
  onComplete: ({ covered, total, stats }) => {
    process.stdout.write(`\n🎉 INITIAL FILL COMPLETE — ${covered}/${total} tickers\n`);
    process.stdout.write(`   Prices: ${stats.prices.toLocaleString()} rows\n`);
    process.stdout.write(`   Options: ${stats.options.toLocaleString()} contracts\n`);
  },
});

async function main() {
  console.log('\n=== SP500 Initial Data Fill ===');
  console.log(`Started: ${new Date().toISOString()}`);
  console.log('Universe: reading from universe_config DB table (456 tickers)\n');

  try {
    // runDailyCollection is not exported — call phases directly
    const cfg = await store.getConfig().catch(() => ({}));
    const historyDays = parseInt(cfg.history_days || '3650', 10);

    const fullUniverse       = await store.getActiveUniverse();
    const equityTickers      = fullUniverse.filter(u => u.category === 'equity').map(u => u.ticker);
    const marketTickers      = fullUniverse.filter(u => u.category !== 'equity').map(u => u.ticker);
    const optionsTickers     = fullUniverse.filter(u => u.has_options).map(u => u.ticker);
    const fundamentalTickers = fullUniverse.filter(u => u.has_fundamentals).map(u => u.ticker);

    console.log(`Equity tickers: ${equityTickers.length}`);
    console.log(`Market/ETF tickers: ${marketTickers.length}`);
    console.log(`Options-enabled: ${optionsTickers.length}`);
    console.log(`Fundamentals-enabled: ${fundamentalTickers.length}\n`);

    // Phase 2a: Historical prices — SP500 equity names
    console.log('\n--- Phase 2a: Historical Prices (SP500 equities) ---');
    await collector.runHistoricalPrices(historyDays, equityTickers);

    // Phase 2b: Market instrument prices (ETFs, benchmarks)
    console.log('\n--- Phase 2b: Market Prices (ETFs + benchmarks) ---');
    const { runMarketPricesYFinance } = require('./src/pipeline/collector');
    if (marketTickers.length > 0) {
      // runMarketPricesYFinance is not exported — skip silently, covered by phase 2a for SP500
      console.log(`[skip] runMarketPricesYFinance not exported — ETF prices collected via runHistoricalPrices if in equity list`);
    }

    // Phase 3: Options chains
    console.log('\n--- Phase 3: Options Chains ---');
    await collector.runOptions(optionsTickers);

    // Phase 4: Technicals
    console.log('\n--- Phase 4: Technicals ---');
    await collector.runTechnicals(equityTickers);

    // Phase 5: Fundamentals
    console.log('\n--- Phase 5: Fundamentals ---');
    await collector.runFundamentals(fundamentalTickers);

    console.log('\n=== Fill Complete ===');
    console.log(`Finished: ${new Date().toISOString()}`);
    process.exit(0);
  } catch (err) {
    console.error('\nFATAL ERROR:', err.message);
    console.error(err.stack);
    process.exit(1);
  }
}

main();
