'use strict';

// S&P 500 constituents (as of April 2026)
// Source: SPDR S&P 500 ETF Trust (SPY) holdings
// Refresh via: /refresh-universe in Discord (fetches from provider when endpoint is available)
const SP500 = [
  // Information Technology
  'AAPL','MSFT','NVDA','AVGO','CRM','ORCL','AMD','QCOM','ADBE','TXN',
  'INTU','CSCO','IBM','AMAT','MU','NOW','KLAC','LRCX','ADI','MCHP',
  'PANW','CDNS','SNPS','FTNT','KEYS','TEL','GLW','HPQ','WDC','STX',
  'NTAP','GDDY','CDW','ZBRA','SWKS','FFIV','JNPR','FSLR','EPAM','PTC',
  'TER','AKAM','ANSS','LDOS','DXC','HPE','ENPH','GEN','MPWR','VRSN',
  // Communication Services
  'META','GOOGL','GOOG','NFLX','DIS','CMCSA','T','VZ','CHTR','TMUS',
  'PARA','WBD','FOXA','FOX','LYV','TTWO','EA','NWSA','NWS','OMC',
  'IPG','ZG','MTCH','ATVI',
  // Consumer Discretionary
  'AMZN','TSLA','HD','MCD','NKE','LOW','SBUX','TJX','BKNG','ABNB',
  'CMG','MAR','HLT','YUM','DRI','ROST','ORLY','AZO','BBY','EXPE',
  'MGM','CZR','RCL','CCL','NCLH','PHM','DHI','LEN','NVR','TOL',
  'F','GM','APTV','BWA','LKQ','GPC','AN','KMX','POOL','VFC',
  'PVH','HAS','ETSY','EBAY','CPRI','TPR','RL','DECK','UAA',
  // Consumer Staples
  'WMT','PG','COST','KO','PEP','PM','MO','MDLZ','CL','GIS',
  'KHC','KMB','SYY','HSY','MKC','TSN','HRL','CAG','CPB','K',
  'CLX','CHD','EL','COTY','SPB',
  // Health Care
  'LLY','UNH','JNJ','ABBV','MRK','TMO','ABT','DHR','BMY','AMGN',
  'GILD','VRTX','ISRG','SYK','MDT','BSX','ZTS','REGN','BIIB','EW',
  'IDXX','MTD','BAX','ALGN','HOLX','DGX','LH','IQV','CRL','HSIC',
  'TECH','PKI','GEHC','HZNP','NTRA','RMD','COO','STE','PODD','DXCM',
  'INCY','EXAS','SGEN','MRNA','PFE','CVS','CI','HUM','ELV','CNC',
  'MOH','DVA',
  // Financials
  'BRK-B','JPM','V','MA','BAC','WFC','GS','MS','C','AXP',
  'BLK','SCHW','CB','PNC','USB','TFC','COF','AIG','MET','PRU',
  'ALL','TRV','AFL','MCO','SPGI','ICE','CME','NDAQ','CBOE','FDS',
  'MSCI','WTW','AON','MMC','BRO','CINF','GL','LNC','UNM','FG',
  'RJF','STT','NTRS','BK','HBAN','CFG','MTB','RF','KEY','ZION',
  'CMA','FHN','FITB','WAL','EWBC','BOH',
  // Industrials
  'CAT','GE','HON','RTX','LMT','UPS','BA','DE','ETN','EMR',
  'ITW','GD','NOC','FDX','MMM','CSX','NSC','UNP','WM','RSG',
  'PCAR','CTAS','ROK','AME','PH','DOV','ROP','FAST','GWW','MSC',
  'IR','XYL','IEX','OTIS','CARR','TT','LII','JCI','VRT','HUBB',
  'AOS','CHRW','JBHT','EXPD','FW','AXTA','HII','TDG','SPR','AIR',
  'J','LDOS','SAIC','GVA','PWR','MTZ','URI','AGCO',
  // Energy
  'XOM','CVX','COP','SLB','EOG','PSX','VLO','MPC','PXD','OXY',
  'HAL','DVN','BKR','HES','CTRA','APA','MRO','FANG','EQT','OKE',
  'WMB','KMI','LNG','ET','EPD',
  // Materials
  'LIN','APD','SHW','FCX','NEM','NUE','VMC','MLM','PKG','IP',
  'CF','MOS','ALB','CE','EMN','RPM','SEE','AVY','SON','CCK',
  'BLL','AMCR','WRK','IFF','PPG','ECL','DD','CTVA',
  // Real Estate
  'AMT','PLD','EQIX','CCI','SPG','PSA','O','WELL','EXR','AVB',
  'EQR','ARE','DLR','VTR','BXP','SLG','KIM','MAA','CPT','NNN',
  'FR','CUBE','INVH','AMH','TRNO','REXR',
  // Utilities
  'NEE','DUK','SO','D','SRE','AEP','XEL','EXC','WEC','ETR',
  'ES','FE','CMS','AEE','LNT','CNP','EVRG','NI','PNW','NRG',
  // Benchmarks always collected
  'SPY','QQQ','IWM','DIA',
];

// Legacy alias — kept for any code that still imports SP100
const SP100 = SP500;

// Benchmark indices / ETFs tracked alongside universe
const BENCHMARKS = ['SPY','QQQ','IWM','DIA','VIX'];

// Sector ETFs for regime context
const SECTOR_ETFS = ['XLK','XLF','XLE','XLV','XLI','XLY','XLP','XLRE','XLB','XLU','XLC'];

function getUniverse(type = 'SP500') {
  switch (type) {
    case 'SP500':
    case 'SP100':      return [...new Set(SP500)];  // deduplicated
    case 'benchmarks': return [...BENCHMARKS];
    case 'sectors':    return [...SECTOR_ETFS];
    case 'all':        return [...new Set([...SP500, ...BENCHMARKS, ...SECTOR_ETFS])];
    default:           return [...new Set(SP500)];
  }
}

// Batch tickers into groups of N (for multi-ticker API calls)
function batch(tickers, size = 20) {
  const batches = [];
  for (let i = 0; i < tickers.length; i += size) {
    batches.push(tickers.slice(i, i + size));
  }
  return batches;
}

module.exports = { SP500, SP100, BENCHMARKS, SECTOR_ETFS, getUniverse, batch };
