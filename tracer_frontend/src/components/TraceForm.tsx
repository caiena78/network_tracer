import React, { useState } from 'react';
import { Play, X, RefreshCw, RotateCcw } from 'lucide-react';
import { useTraceStore } from '../store/traceStore';
import { clearTraceCache } from '../api/client';

// ---------------------------------------------------------------------------
// IP validation (IPv4 + IPv6)
// ---------------------------------------------------------------------------

function isValidIp(value: string): boolean {
  const v = value.trim();
  // IPv4
  const ipv4 = /^(\d{1,3}\.){3}\d{1,3}$/.test(v) &&
    v.split('.').every((o) => parseInt(o, 10) <= 255);
  if (ipv4) return true;
  // IPv6 (rough)
  const ipv6 = /^([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}$/.test(v);
  return ipv6;
}

// ---------------------------------------------------------------------------
// Progress log
// ---------------------------------------------------------------------------

function ProgressLog({ lines }: { lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <div
      style={{
        marginTop: '12px',
        background: 'var(--bg-code)',
        border: '1px solid var(--border-color)',
        borderRadius: '6px',
        padding: '8px 10px',
        maxHeight: '140px',
        overflowY: 'auto',
      }}
    >
      {lines.map((line, i) => (
        <div
          key={i}
          style={{
            fontSize: '11px',
            fontFamily: 'monospace',
            color: 'var(--text-muted)',
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {line}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TraceForm
// ---------------------------------------------------------------------------

export default function TraceForm() {
  const {
    srcIp, dstIp,
    phase, progress, error,
    setSrcIp, setDstIp,
    runTrace, cancelTrace, clearTrace,
  } = useTraceStore();

  const [srcError, setSrcError] = useState('');
  const [dstError, setDstError] = useState('');

  const validate = () => {
    let ok = true;
    if (!srcIp.trim()) {
      setSrcError('Source IP is required');
      ok = false;
    } else if (!isValidIp(srcIp)) {
      setSrcError('Enter a valid IPv4 or IPv6 address');
      ok = false;
    } else {
      setSrcError('');
    }
    if (!dstIp.trim()) {
      setDstError('Destination IP is required');
      ok = false;
    } else if (!isValidIp(dstIp)) {
      setDstError('Enter a valid IPv4 or IPv6 address');
      ok = false;
    } else {
      setDstError('');
    }
    return ok;
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) return;
    void runTrace();
  };

  const isRunning   = phase === 'submitting' || phase === 'streaming';
  const isEnriching = phase === 'enriching';
  const isDone      = phase === 'done';
  const isError     = phase === 'error';
  const isIdle      = phase === 'idle';

  const [clearing, setClearing] = useState(false);

  const handleClearAndRerun = async () => {
    setClearing(true);
    try {
      await clearTraceCache(srcIp, dstIp);
    } catch {
      // Ignore cache-clear errors — run fresh regardless
    } finally {
      setClearing(false);
    }
    void runTrace();
  };

  return (
    <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '0' }}>
      <div style={{ padding: '16px 16px 0' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.08em', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '12px' }}>
          Network Trace
        </div>

        {/* Source IP */}
        <div style={{ marginBottom: '10px' }}>
          <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '4px' }}>
            Source IP
          </label>
          <input
            type="text"
            value={srcIp}
            onChange={(e) => { setSrcIp(e.target.value.trim()); setSrcError(''); }}
            placeholder="e.g. 10.254.29.194"
            disabled={isRunning}
            className="form-input"
            style={{ width: '100%' }}
          />
          {srcError && (
            <div style={{ fontSize: '11px', color: 'var(--color-error)', marginTop: '3px' }}>{srcError}</div>
          )}
        </div>

        {/* Destination IP */}
        <div style={{ marginBottom: '12px' }}>
          <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '4px' }}>
            Destination IP
          </label>
          <input
            type="text"
            value={dstIp}
            onChange={(e) => { setDstIp(e.target.value.trim()); setDstError(''); }}
            placeholder="e.g. 172.28.248.118"
            disabled={isRunning}
            className="form-input"
            style={{ width: '100%' }}
          />
          {dstError && (
            <div style={{ fontSize: '11px', color: 'var(--color-error)', marginTop: '3px' }}>{dstError}</div>
          )}
        </div>

        {/* Action buttons */}
        <div style={{ display: 'flex', gap: '8px' }}>
          {!isRunning ? (
            <button
              type={isDone ? 'button' : 'submit'}
              onClick={isDone ? handleClearAndRerun : undefined}
              disabled={clearing}
              className="btn btn-primary"
              style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}
            >
              {isError ? (
                <><RefreshCw size={14} />Run Again</>
              ) : isDone ? (
                <><RefreshCw size={14} />{clearing ? 'Clearing cache…' : 'Re-run Trace'}</>
              ) : (
                <><Play size={14} />Run Trace</>
              )}
            </button>
          ) : (
            <button
              type="button"
              onClick={cancelTrace}
              className="btn btn-secondary"
              style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}
            >
              <X size={14} />
              Cancel
            </button>
          )}

          {(isDone || isError) && (
            <button
              type="button"
              onClick={clearTrace}
              className="btn btn-ghost"
              title="Clear diagram"
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 10px' }}
            >
              <RotateCcw size={14} />
            </button>
          )}
        </div>
      </div>

      {/* Status indicators */}
      {isRunning && (
        <div style={{ padding: '12px 16px 0', display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--color-primary)' }}>
          <span className="spinner" />
          <span style={{ fontSize: '12px' }}>
            {phase === 'submitting' ? 'Submitting trace…' : 'Collecting path topology…'}
          </span>
        </div>
      )}
      {isEnriching && (
        <div style={{ padding: '12px 16px 0', display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--color-success)' }}>
          <span className="spinner" style={{ borderTopColor: 'var(--color-success)' }} />
          <span style={{ fontSize: '12px' }}>Graph drawn — enriching interface counters…</span>
        </div>
      )}

      {/* Error banner */}
      {isError && error && (
        <div style={{ margin: '12px 16px 0', padding: '10px 12px', background: 'var(--color-error-bg)', border: '1px solid var(--color-error-border)', borderRadius: '6px', fontSize: '12px', color: 'var(--color-error)' }}>
          {error}
        </div>
      )}

      {/* Progress log */}
      {progress.length > 0 && (
        <div style={{ padding: '0 16px' }}>
          <ProgressLog lines={progress} />
        </div>
      )}

      {/* Success indicator */}
      {isDone && (
        <div style={{ margin: '12px 16px 0', padding: '8px 12px', background: 'var(--color-success-bg)', border: '1px solid var(--color-success-border)', borderRadius: '6px', fontSize: '12px', color: 'var(--color-success)' }}>
          Trace complete — diagram rendered below.
        </div>
      )}

      <div style={{ height: '16px' }} />
    </form>
  );
}
