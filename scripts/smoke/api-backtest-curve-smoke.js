const http = require('http');
require('../src/channels/api/server.js');
setTimeout(() => {
  http.get('http://localhost:' + (process.env.DASHBOARD_PORT || '3000') + '/api/strategies/' +
    encodeURIComponent(process.env.SMOKE_SID || 'S_idiosyncratic_vol_puzzle') + '/backtest-curve', res => {
    let b=''; res.on('data',d=>b+=d); res.on('end',()=>{
      const j=JSON.parse(b);
      if (!Array.isArray(j.rows)) { console.error('BAD shape'); process.exit(1); }
      if (j.rows.length && !('spx_equity' in j.rows[0])) { console.error('missing spx_equity'); process.exit(1); }
      console.log('ok rows='+j.rows.length); process.exit(0);
    });
  }).on('error', e => { console.error('REQ ERR', e.message); process.exit(1); });
}, 2500);
