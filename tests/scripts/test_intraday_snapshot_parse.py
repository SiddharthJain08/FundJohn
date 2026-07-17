import subprocess, pathlib
ROOT = pathlib.Path('/root/openclaw')

def test_snapshot_dailybar_to_row_via_node():
    script = r'''
      const { _snapshotToPriceRow } = require('./src/pipeline/collector.js');
      const snap = { dailyBar: { o:743.63, h:746.9, l:722.59, c:733.96, v:63273993 } };
      const row = _snapshotToPriceRow('SPY', snap, '2026-06-09');
      const ok = row && row.ticker==='SPY' && row.date==='2026-06-09'
                 && row.close===733.96 && row.open===743.63 && row.high===746.9
                 && row.low===722.59 && row.volume===63273993;
      const none = _snapshotToPriceRow('NODAILY', { dailyBar: null }, '2026-06-09');
      if (!ok) { console.error('row mismatch', JSON.stringify(row)); process.exit(1); }
      if (none !== null) { console.error('expected null for missing dailyBar'); process.exit(1); }
      console.log('OK');
    '''
    r = subprocess.run(['node','-e',script], cwd=str(ROOT), capture_output=True, text=True)
    assert 'OK' in r.stdout, r.stdout + r.stderr
