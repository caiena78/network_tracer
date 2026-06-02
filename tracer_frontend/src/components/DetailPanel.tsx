import React, { useCallback, useEffect, useRef, useState } from 'react';
import { X, ExternalLink, RefreshCw } from 'lucide-react';
import type { OrdrDeviceData, SelectedElement, NodeData, EdgeData, StackMember } from '../types/trace';
import { useTraceStore } from '../store/traceStore';
import { fetchInterfaceDetail, queryOrdr } from '../api/client';
import { InterfaceCounters, RoutingSection } from './ConnectionEdge';

// ---------------------------------------------------------------------------
// Resize hook — drag the LEFT edge of a right-side panel
// ---------------------------------------------------------------------------

const PANEL_MIN     = 240;
const PANEL_MAX     = 800;
const PANEL_DEFAULT = 300;
const PANEL_STORAGE = 'tracer-detail-panel-width';

function usePanelResize() {
  const [width, setWidth] = useState<number>(() => {
    try {
      const s = localStorage.getItem(PANEL_STORAGE);
      if (s) { const n = parseInt(s, 10); if (n >= PANEL_MIN && n <= PANEL_MAX) return n; }
    } catch { /* ignore */ }
    return PANEL_DEFAULT;
  });

  const isResizing  = useRef(false);
  const startX      = useRef(0);
  const startWidth  = useRef(0);
  const latestWidth = useRef(width);
  useEffect(() => { latestWidth.current = width; }, [width]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isResizing.current = true;
    startX.current     = e.clientX;
    startWidth.current = latestWidth.current;
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!isResizing.current) return;
      // Moving the mouse LEFT (negative delta) widens the panel
      const delta    = e.clientX - startX.current;
      const newWidth = Math.min(Math.max(startWidth.current - delta, PANEL_MIN), PANEL_MAX);
      setWidth(newWidth);
    };
    const onUp = () => {
      if (!isResizing.current) return;
      isResizing.current             = false;
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
      try { localStorage.setItem(PANEL_STORAGE, String(latestWidth.current)); } catch { /* ignore */ }
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
    return () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
    };
  }, []);

  return { width, onMouseDown };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function Row({ label, value, warn }: { label: string; value: string | number | null | undefined; warn?: boolean }) {
  if (value == null || value === '') return null;
  return (
    <tr>
      <td style={{ padding: '3px 8px 3px 0', color: 'var(--text-muted)', fontSize: '12px', whiteSpace: 'nowrap', verticalAlign: 'top' }}>
        {label}
      </td>
      <td style={{ padding: '3px 0', color: warn ? 'var(--color-warning)' : 'var(--text-primary)', fontSize: '12px', fontWeight: warn ? 600 : 400, wordBreak: 'break-all' }}>
        {String(value)}
      </td>
    </tr>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: '14px' }}>
      <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.08em', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '6px' }}>
        {title}
      </div>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// On-demand fetch button
// ---------------------------------------------------------------------------

function FetchButton({
  device,
  iface,
  deviceIpMap,
}: {
  device:      string;
  iface:       string;
  deviceIpMap: Record<string, string>;
}) {
  const ip          = deviceIpMap[device];
  const cacheResult = useTraceStore((s) => s.cacheInterfaceDetail);
  const hasCache    = useTraceStore((s) => !!s.interfaceDetailCache[`${device}/${iface}`]);
  const [busy,  setBusy]  = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!ip) return null;

  const handleClick = async () => {
    setBusy(true);
    setError(null);
    try {
      // Always fetch live data from the device — never skip due to cached data
      const result = await fetchInterfaceDetail(ip, iface);
      // Overwrite cache with the fresh result
      cacheResult(device, iface, result);
      // Also update the diagram edge chip counters
      useTraceStore.setState((s) => ({
        pendingEnrichments: [
          ...s.pendingEnrichments,
          { device, interface: iface, data: { ...result.parsed, raw_output: result.raw_output } },
        ],
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Fetch failed');
    } finally {
      setBusy(false);
    }
  };

  const btnLabel = busy
    ? 'Fetching live data…'
    : hasCache
    ? `Refresh — ${device} ${iface}`
    : `Get interface details — ${device} ${iface}`;

  return (
    <div style={{ marginTop: '6px' }}>
      <button
        onClick={() => void handleClick()}
        disabled={busy}
        className="btn btn-secondary"
        style={{ fontSize: '12px', padding: '5px 10px', display: 'flex', alignItems: 'center', gap: '6px', width: '100%', justifyContent: 'center' }}
      >
        <RefreshCw size={12} className={busy ? 'spin' : ''} />
        {btnLabel}
      </button>
      {error && (
        <div style={{ fontSize: '11px', color: 'var(--color-error)', marginTop: '4px' }}>{error}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Raw output block
// ---------------------------------------------------------------------------

function RawBlock({ title, text }: { title: string; text: string }) {
  return (
    <div style={{ marginBottom: '10px' }}>
      {title && (
        <div style={{ fontSize: '10px', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '4px' }}>
          {title}
        </div>
      )}
      <pre
        style={{
          margin:       0,
          padding:      '6px 8px',
          background:   'var(--bg-code)',
          borderRadius: '4px',
          fontSize:     '10px',
          fontFamily:   'monospace',
          color:        'var(--text-primary)',
          whiteSpace:   'pre',
          overflowX:    'auto',
          maxHeight:    '260px',
          overflowY:    'auto',
        }}
      >
        {text}
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ORDR data panel
// ---------------------------------------------------------------------------

const RISK_COLORS: Record<string, string> = {
  HIGH:     'var(--color-error)',
  MEDIUM:   'var(--color-warning)',
  LOW:      'var(--color-success)',
  CRITICAL: 'var(--color-error)',
};

function Badge({ label, color }: { label: string; color: string }) {
  return (
    <span style={{
      padding: '2px 8px', borderRadius: '12px', fontSize: '11px', fontWeight: 700,
      background: 'var(--bg-code)',
      border: `1px solid ${color}`,
      color,
    }}>
      {label}
    </span>
  );
}

function OrdrPanel({ ip }: { ip: string }) {
  const [data,    setData]    = useState<OrdrDeviceData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);
  const [open,    setOpen]    = useState(false);

  const handleFetch = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await queryOrdr(ip);
      setData(result);
      setOpen(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'ORDR query failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ marginTop: '12px', borderTop: '1px solid var(--border-color)', paddingTop: '10px' }}>
      <button
        onClick={() => void handleFetch()}
        disabled={loading}
        className="btn btn-secondary"
        style={{ width: '100%', justifyContent: 'center', gap: '7px', fontSize: '12px', padding: '6px 10px', display: 'flex', alignItems: 'center' }}
      >
        <RefreshCw size={13} className={loading ? 'spin' : ''} />
        {loading ? 'Querying ORDR…' : data ? 'Refresh ORDR Data' : 'Get ORDR Data'}
      </button>

      {error && (
        <div style={{ fontSize: '11px', color: 'var(--color-error)', marginTop: '6px' }}>{error}</div>
      )}

      {data && open && (
        <div style={{ marginTop: '10px' }}>
          {/* Header */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
            <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.07em', color: 'var(--text-muted)', textTransform: 'uppercase' }}>
              ORDR Device Record
            </div>
            <button className="btn btn-ghost" style={{ padding: '2px' }} onClick={() => setOpen(false)}>
              <X size={13} />
            </button>
          </div>

          {/* Status badges row */}
          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '10px' }}>
            {data.conn_status && (
              <Badge
                label={data.conn_status}
                color={data.conn_status === 'ONLINE' ? 'var(--color-success)' : 'var(--color-error)'}
              />
            )}
            {data.risk_state && (
              <Badge
                label={`Risk: ${data.risk_state}${data.risk_score != null ? ` (${data.risk_score})` : ''}`}
                color={RISK_COLORS[data.risk_state.toUpperCase()] ?? 'var(--text-muted)'}
              />
            )}
            {data.known_vuln_risk && data.known_vuln_risk !== 'NONE' && (
              <Badge
                label={`Vuln: ${data.known_vuln_risk}`}
                color={RISK_COLORS[data.known_vuln_risk.toUpperCase()] ?? 'var(--text-muted)'}
              />
            )}
            {data.alarm_count != null && data.alarm_count > 0 && (
              <Badge label={`${data.alarm_count} alarm${data.alarm_count !== 1 ? 's' : ''}`} color="var(--color-warning)" />
            )}
          </div>

          <OrdrGroup title="Identity">
            <Row label="Device name"   value={data.device_name} />
            <Row label="FQDN"          value={data.fqdn} />
            <Row label="DHCP hostname" value={data.dhcp_hostname} />
            <Row label="MAC"           value={data.mac} />
            <Row label="IP"            value={data.ip} />
            <Row label="Serial"        value={data.serial} />
            <Row label="First seen"    value={data.first_seen} />
            <Row label="Last seen"     value={data.last_seen} />
          </OrdrGroup>

          <OrdrGroup title="Classification">
            <Row label="Type"          value={data.device_type} />
            <Row label="Description"   value={data.device_descr} />
            <Row label="Group"         value={data.group} />
            <Row label="Profile"       value={data.profile} />
            <Row label="Endpoint type" value={data.endpoint_type} />
            <Row label="Classified"    value={data.classification_state} />
            <Row label="Criticality"   value={data.criticality} />
            <Row label="OU"            value={data.ou} />
            <Row label="FDA class"     value={data.fda_class != null ? String(data.fda_class) : undefined} />
            <Row label="Secondary"     value={data.secondary_device != null ? (data.secondary_device ? 'Yes' : 'No') : undefined} />
            <Row label="Guest"         value={data.guest_device != null ? (data.guest_device ? 'Yes' : 'No') : undefined} />
          </OrdrGroup>

          <OrdrGroup title="Hardware / Software">
            <Row label="Manufacturer"  value={data.manufacturer || data.mfg_name} />
            <Row label="Model"         value={data.model} />
            <Row label="OS"            value={data.os_type ? `${data.os_type}${data.os_version ? ` ${data.os_version}` : ''}` : data.os_version} />
            <Row label="SW version"    value={data.sw_version} />
          </OrdrGroup>

          <OrdrGroup title="Network">
            <Row label="Subnet"        value={data.subnet} />
            <Row label="VLAN"          value={data.vlan != null ? `${data.vlan}${data.vlan_name ? ` — ${data.vlan_name}` : ''}` : undefined} />
            <Row label="Access"        value={data.access_type} />
            <Row label="SSID"          value={data.essid} />
            <Row label="DHCP"          value={data.dhcp_enabled != null ? (data.dhcp_enabled ? `Enabled${data.dhcp_hostname ? ` (${data.dhcp_hostname})` : ''}` : 'Disabled') : undefined} />
          </OrdrGroup>

          <OrdrGroup title="Connected via">
            <Row label="Switch"        value={data.nw_equip_hostname} />
            <Row label="Interface"     value={data.nw_equip_interface} />
            <Row label="Scrape IP"     value={data.nw_equip_scrape_ip} />
            <Row label="Sensor"        value={data.sensor_name ? `${data.sensor_name}${data.sensor_ip ? ` (${data.sensor_ip})` : ''}` : undefined} />
          </OrdrGroup>

          {(data.device_location || data.sensor_location) && (
            <OrdrGroup title="Location">
              <Row label="Device"      value={data.device_location} />
              <Row label="Sensor"      value={data.sensor_location} />
            </OrdrGroup>
          )}

          <OrdrGroup title="Risk &amp; Security">
            <Row label="Risk score"    value={data.risk_score != null ? String(data.risk_score) : undefined} warn={(data.risk_score ?? 0) > 5} />
            <Row label="Known vulns"   value={data.known_vuln_risk} warn={data.known_vuln_risk === 'CRITICAL' || data.known_vuln_risk === 'HIGH'} />
            <Row label="Alarms"        value={data.alarm_count != null ? String(data.alarm_count) : undefined} warn={(data.alarm_count ?? 0) > 0} />
            <Row label="PHI"           value={data.has_phi != null ? (data.has_phi ? 'Yes' : 'No') : undefined} warn={!!data.has_phi} />
            <Row label="Ext flows"     value={data.has_external_flows} warn={data.has_external_flows === 'true'} />
            <Row label="Blacklisted"   value={data.is_blacklisted != null ? (data.is_blacklisted ? 'Yes' : 'No') : undefined} warn={!!data.is_blacklisted} />
            <Row label="Proxied"       value={data.proxied != null ? (data.proxied ? 'Yes' : 'No') : undefined} />
          </OrdrGroup>
        </div>
      )}
    </div>
  );
}

function OrdrGroup({ title, children }: { title: string; children: React.ReactNode }) {
  const kids = React.Children.toArray(children).filter(Boolean);
  if (kids.length === 0) return null;
  return (
    <div style={{ marginBottom: '8px' }}>
      <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.07em', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '3px' }}>
        {title}
      </div>
      <table style={{ borderCollapse: 'collapse', width: '100%' }}>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Node detail
// ---------------------------------------------------------------------------

function NodeDetail({ data }: { data: NodeData }) {
  const members     = Array.isArray(data.stack_members) ? (data.stack_members as StackMember[]) : [];
  const deviceIpMap = useTraceStore((s) => s.graph?.metadata?.device_ip_map ?? {});
  // Use the node's direct IP first (src/dst endpoints), then the management IP from the device map
  const displayIp   = (data.ip as string | undefined) || deviceIpMap[data.label] || null;
  const queryIp     = displayIp;

  return (
    <>
      <Section title="Device">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Name"        value={data.label} />
            <Row label="IP"          value={displayIp ?? undefined} />
            <Row label="Type"        value={data.node_type} />
            <Row label="Layer"       value={data.layer} />
            <Row label="Description" value={data.description} />
            <Row label="State"       value={data.state} />
            <Row label="Speed"       value={data.speed} />
            <Row label="Duplex"      value={data.duplex} />
            <Row label="Version"     value={data.os_version as string | undefined} />
            <Row label="Uptime"      value={data.uptime as string | undefined} />
          </tbody>
        </table>
      </Section>

      {/* Stack member details */}
      {members.length > 0 && (
        <Section title={`Stack Members (${members.length})`}>
          {members.map((mb) => (
            <div
              key={mb.switch_num}
              style={{
                marginBottom: '8px',
                padding: '8px',
                background: 'var(--bg-code)',
                borderRadius: '4px',
                borderLeft: `3px solid ${mb.role?.toUpperCase().includes('ACTIVE') ? 'var(--color-success)' : 'var(--border-color)'}`,
              }}
            >
              <div style={{ fontWeight: 700, fontSize: '12px', marginBottom: '4px', color: 'var(--text-primary)' }}>
                Switch {mb.switch_num}
                {mb.role && (
                  <span style={{
                    marginLeft: '8px',
                    fontSize: '10px',
                    fontWeight: 700,
                    color: mb.role.toUpperCase().includes('ACTIVE') ? 'var(--color-success)' : 'var(--text-muted)',
                    textTransform: 'uppercase',
                  }}>
                    {mb.role}
                  </span>
                )}
              </div>
              <table style={{ borderCollapse: 'collapse', width: '100%' }}>
                <tbody>
                  {mb.model      && <Row label="Model"   value={mb.model} />}
                  {mb.os_version && <Row label="Version" value={mb.os_version} />}
                  {mb.uptime     && <Row label="Uptime"  value={mb.uptime} />}
                  {mb.serial     && <Row label="Serial"  value={mb.serial} />}
                  {mb.mac        && <Row label="MAC"     value={mb.mac} />}
                </tbody>
              </table>
            </div>
          ))}
        </Section>
      )}

      {data.netbox_url && (
        <a href={data.netbox_url as string} target="_blank" rel="noopener noreferrer" className="netbox-link">
          <ExternalLink size={12} />
          View device in NetBox
        </a>
      )}

      {/* ORDR device intelligence — shown when we have an IP to query */}
      {queryIp && <OrdrPanel ip={queryIp} />}
    </>
  );
}

// ---------------------------------------------------------------------------
// Edge detail
// ---------------------------------------------------------------------------

function EdgeDetail({ data }: { data: EdgeData }) {
  const deviceIpMap = useTraceStore((s) => s.graph?.metadata?.device_ip_map ?? {});
  // Read from shared store cache — persists across unmounts and sidebar re-opens
  const cache = useTraceStore((s) => s.interfaceDetailCache);

  const srcKey = `${data.src_device}/${data.src_interface}`;
  const dstKey = `${data.dst_device}/${data.dst_interface}`;
  const srcRaw = cache[srcKey]?.raw_output ?? data.src_raw_output;
  const dstRaw = cache[dstKey]?.raw_output ?? data.dst_raw_output;

  // hasErrors drives the FetchButton label style but the actual counter display
  // is handled by InterfaceCounters — so this just checks if any side has errors.
  const chk = (f: string) => ((data as Record<string,unknown>)[f] as number ?? 0) > 0;
  const hasErrors = ['src_crc','dst_crc','src_input_error','dst_input_error',
    'src_runts','dst_runts','src_output_error','dst_output_error',
    'crc','input_error','runts','output_error'].some(chk);

  return (
    <>
      {/* Source side */}
      {(data.src_device || data.src_interface) && (
        <Section title="Source">
          <table style={{ borderCollapse: 'collapse', width: '100%' }}>
            <tbody>
              <Row label="Device"    value={data.src_device} />
              <Row label="Interface" value={data.src_interface} />
            </tbody>
          </table>
          {data.src_interface_netbox_url && (
            <a href={data.src_interface_netbox_url} target="_blank" rel="noopener noreferrer" className="netbox-link">
              <ExternalLink size={12} /> Source interface in NetBox
            </a>
          )}
          {data.src_device && data.src_interface && (
            <FetchButton
              device={data.src_device}
              iface={data.src_interface}
              deviceIpMap={deviceIpMap}
            />
          )}
        </Section>
      )}

      {/* Destination side */}
      <Section title="Switch Port">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Device"      value={data.dst_device} />
            <Row label="Interface"   value={data.dst_interface} />
            <Row label="Description" value={data.description as string | undefined} />
            <Row label="Speed"       value={data.speed as string | undefined} />
            <Row label="Duplex"      value={data.duplex as string | undefined} />
            <Row label="VLAN"        value={data.vlan != null ? String(data.vlan) : undefined} />
            <Row label="State"       value={data.state as string | undefined} />
          </tbody>
        </table>
        {data.dst_interface_netbox_url && (
          <a href={data.dst_interface_netbox_url} target="_blank" rel="noopener noreferrer" className="netbox-link">
            <ExternalLink size={12} /> Destination interface in NetBox
          </a>
        )}
        {data.dst_device && data.dst_interface && (
          <FetchButton
            device={data.dst_device}
            iface={data.dst_interface}
            deviceIpMap={deviceIpMap}
          />
        )}
      </Section>

      {/* L3 routing information */}
      <RoutingSection data={data} />

      {/* Link */}
      <Section title="Link">
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <tbody>
            <Row label="Layer" value={data.layer} />
          </tbody>
        </table>
      </Section>

      {/* Per-side interface counters */}
      <InterfaceCounters
        data={data}
        side="src"
        title={`Egress — ${data.src_device ?? ''} ${data.src_interface ?? ''}`}
      />
      <InterfaceCounters
        data={data}
        side="dst"
        title={`Ingress — ${data.dst_device ?? ''} ${data.dst_interface ?? ''}`}
      />

      {/* Raw show interface output */}
      {(srcRaw || dstRaw) && (
        <Section title="Show Interface">
          {srcRaw && data.src_interface && (
            <RawBlock
              title={dstRaw ? `${data.src_device} / ${data.src_interface}` : ''}
              text={srcRaw}
            />
          )}
          {dstRaw && data.dst_interface && (
            <RawBlock
              title={srcRaw ? `${data.dst_device} / ${data.dst_interface}` : ''}
              text={dstRaw}
            />
          )}
        </Section>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// DetailPanel
// ---------------------------------------------------------------------------

export default function DetailPanel() {
  const selectedElement    = useTraceStore((s) => s.selectedElement);
  const setSelectedElement = useTraceStore((s) => s.setSelectedElement);
  const { width, onMouseDown: startResize } = usePanelResize();

  if (!selectedElement) return null;

  const title =
    selectedElement.type === 'node'
      ? selectedElement.data.label
      : `${(selectedElement.data as EdgeData).src_device ?? '?'} → ${(selectedElement.data as EdgeData).dst_device ?? '?'}`;

  return (
    <div
      style={{
        width:         width,
        minWidth:      PANEL_MIN,
        maxWidth:      PANEL_MAX,
        flexShrink:    0,
        borderLeft:    '1px solid var(--border-color)',
        background:    'var(--bg-panel)',
        display:       'flex',
        flexDirection: 'column',
        overflow:      'hidden',
        position:      'relative',
      }}
    >
      {/* Drag handle — left edge */}
      <div
        onMouseDown={startResize}
        title="Drag to resize panel"
        style={{
          position:   'absolute',
          top:        0,
          left:       0,
          bottom:     0,
          width:      '5px',
          cursor:     'col-resize',
          zIndex:     30,
          background: 'transparent',
          transition: 'background 0.15s',
        }}
        onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = 'var(--color-primary)'; }}
        onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = 'transparent'; }}
      />
      {/* Header */}
      <div
        style={{
          padding:       '12px 14px',
          borderBottom:  '1px solid var(--border-color)',
          display:       'flex',
          alignItems:    'center',
          gap:           '8px',
        }}
      >
        <div
          style={{
            flex:           1,
            fontSize:       '13px',
            fontWeight:     700,
            color:          'var(--text-primary)',
            overflow:       'hidden',
            textOverflow:   'ellipsis',
            whiteSpace:     'nowrap',
          }}
        >
          {title}
        </div>
        <button
          onClick={() => setSelectedElement(null)}
          className="btn btn-ghost"
          style={{ padding: '3px', flexShrink: 0 }}
          title="Close"
        >
          <X size={14} />
        </button>
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '14px' }}>
        {selectedElement.type === 'node' ? (
          <NodeDetail data={selectedElement.data as NodeData} />
        ) : (
          <EdgeDetail data={selectedElement.data as EdgeData} />
        )}
      </div>
    </div>
  );
}
