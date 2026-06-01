// TypeScript interfaces aligned to the tracer_api backend schema.
// Source of truth: tracer_api/models.py and tracer_api/graph_builder.py

// ---------------------------------------------------------------------------
// Trace execution
// ---------------------------------------------------------------------------

export type TraceStatus =
  | 'pending'
  | 'running'
  | 'enriching'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface InterfaceUpdate {
  device:    string;
  interface: string;
  data:      Record<string, unknown>;
}

export interface DeviceUpdate {
  device: string;
  data: {
    os_version?:    string | null;
    uptime?:        string | null;
    stack_members?: StackMember[];
  };
}

export interface PortchannelUpdate {
  device:    string;
  interface: string;
  members:   string[];
}

export interface TraceSummary {
  trace_id:   string;
  status:     TraceStatus;
  src_ip:     string;
  dst_ip:     string;
  created_at: string;
  updated_at: string;
}

export interface TraceResponse extends TraceSummary {
  progress:         string[];
  result:           FlatPath[] | null;
  error:            string | null;
  duration_seconds: number | null;
}

// ---------------------------------------------------------------------------
// Flat path (raw trace result — one ECMP path)
// ---------------------------------------------------------------------------

export interface HopDetails {
  vlan?:                    number;
  state?:                   string;
  description?:             string;
  speed?:                   string;
  duplex?:                  string;
  runts?:                   number;
  giants?:                  number;
  crc?:                     number;
  input_error?:             number;
  total_output_drops?:      number;
  output_error?:            number;
  output_discard?:          number | null;
  unknown_protocol_drops?:  number | null;
  egress_interface?:        string;
  gateway_ip?:              string;
  next_hop_ip?:             string;
  egress_iface?:            string;
  prefix?:                  string;
  route_source?:            string;
  route_tag?:               string;
  route_age?:               string;
  [key: string]: unknown;
}

export interface Hop {
  layer:     'L2' | 'L3';
  device:    string;
  interface: string;
  details:   HopDetails;
}

export interface FlatPath {
  src_ip:     string;
  dst_ip:     string;
  gateway_ip: string;
  path:       Hop[];
}

// ---------------------------------------------------------------------------
// Graph (Cytoscape.js-compatible, returned by /graph endpoint)
// ---------------------------------------------------------------------------

export interface StackMember {
  switch_num:  number;
  uptime?:     string;
  model?:      string;
  serial?:     string;
  os_version?: string;
  role?:       string;
  mac?:        string;
}

export interface NodeData {
  id:            string;
  label:         string;
  node_type:     'switch' | 'router' | 'gateway' | 'src' | 'dst' | 'unknown';
  layer:         'L2' | 'L3' | 'mixed';
  ip?:           string;
  state?:        string;
  description?:  string;
  speed?:        string;
  duplex?:       string;
  netbox_url?:   string;
  os_version?:   string;
  uptime?:       string;
  stack_members?: StackMember[];
  [key: string]: unknown;
}

export interface EdgeData {
  id:                          string;
  source:                      string;
  target:                      string;
  label:                       string;
  layer:                       'L2' | 'L3' | 'mixed';
  src_device:                  string;
  dst_device:                  string;
  src_interface?:              string | null;
  dst_interface?:              string | null;
  vlan?:                       number;
  state?:                      string;
  runts?:                      number;
  giants?:                     number;
  crc?:                        number;
  input_error?:                number;
  total_output_drops?:         number;
  output_error?:               number;
  src_interface_netbox_url?:   string;
  dst_interface_netbox_url?:   string;
  src_raw_output?:             string;
  dst_raw_output?:             string;
  // L3 routing fields
  prefix?:            string;
  next_hop_ip?:       string;
  egress_iface?:      string;
  route_source?:      string;
  route_tag?:         string;
  route_age?:         string;
  gateway_ip?:        string;
  // BGP-specific
  bgp_as_path?:       string;
  bgp_community?:     string;
  bgp_local_pref?:    number;
  bgp_origin?:        string;
  bgp_med?:           number;
  bgp_weight?:        number;
  [key: string]: unknown;
}

export interface GraphElement {
  data: NodeData | EdgeData;
}

export interface PathInfo {
  path_id:      number;
  edge_ids:     string[];
  src_ip:       string;
  dst_ip:       string;
  gateway_ip:   string;
  ecmp_variant: number;
}

export interface GraphMetadata {
  src_ip:         string;
  dst_ip:         string;
  gateway_ip:     string;
  total_paths:    number;
  netbox_url?:    string | null;
  /** device_name → SSH management IP — used for on-demand show-interface calls */
  device_ip_map?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// ORDR device intelligence
// ---------------------------------------------------------------------------

export interface OrdrDeviceData {
  // Identification
  ip?:                   string;
  mac?:                  string;
  device_name?:          string;
  fqdn?:                 string;
  dhcp_hostname?:        string;
  // Classification
  device_type?:          string;
  device_descr?:         string;
  group?:                string;
  endpoint_type?:        string;
  profile?:              string;
  classification_state?: string;
  // Hardware / Software
  manufacturer?:         string;
  model?:                string;
  os_type?:              string;
  sw_version?:           string;
  serial?:               string;
  // Network
  subnet?:               string;
  vlan?:                 number;
  vlan_name?:            string;
  access_type?:          string;
  dhcp_enabled?:         boolean;
  // Risk
  risk_state?:           string;
  risk_score?:           number;
  known_vuln_risk?:      string;
  criticality?:          string;
  alarm_count?:          number;
  has_phi?:              boolean;
  has_external_flows?:   string;
  // Status
  conn_status?:          string;
  first_seen?:           string;
  last_seen?:            string;
  // Network equipment (where it's connected)
  nw_equip_hostname?:    string;
  nw_equip_interface?:   string;
  nw_equip_scrape_ip?:   string;
  // Sensor
  sensor_name?:          string;
  sensor_ip?:            string;
  [key: string]: unknown;
}

export interface InterfaceDetailResult {
  device_ip:  string;
  interface:  string;
  raw_output: string;
  parsed:     Record<string, unknown>;
}

export interface GraphResponse {
  elements: GraphElement[];
  paths:    PathInfo[];
  metadata: GraphMetadata;
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

export interface HistorySummary {
  id:         string;
  src_ip:     string;
  dst_ip:     string;
  created_at: string;
  status:     string;
  duration_s: number | null;
}

export interface HistoryDetail extends HistorySummary {
  graph: GraphResponse | null;
}

export interface HistoryListResponse {
  entries: HistorySummary[];
  total:   number;
  limit:   number;
  offset:  number;
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

export type SelectedElement =
  | { type: 'node'; data: NodeData }
  | { type: 'edge'; data: EdgeData };

export type AppTheme = 'light' | 'dark';
