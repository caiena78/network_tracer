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
  // Per-side interface counters (src_ = egress port on source device,
  //                              dst_ = ingress port on destination device)
  src_runts?:                  number;
  src_giants?:                 number;
  src_crc?:                    number;
  src_input_error?:            number;
  src_total_output_drops?:     number;
  src_output_error?:           number;
  src_output_discard?:         number;
  src_unknown_protocol_drops?: number;
  src_rx_runts?:               number;
  src_rx_crc?:                 number;
  src_state?:                  string;
  src_speed?:                  string;
  src_duplex?:                 string;
  src_description?:            string;
  dst_runts?:                  number;
  dst_giants?:                 number;
  dst_crc?:                    number;
  dst_input_error?:            number;
  dst_total_output_drops?:     number;
  dst_output_error?:           number;
  dst_output_discard?:         number;
  dst_unknown_protocol_drops?: number;
  dst_rx_runts?:               number;
  dst_rx_crc?:                 number;
  dst_state?:                  string;
  dst_speed?:                  string;
  dst_duplex?:                 string;
  dst_description?:            string;
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

/** Raw field names exactly as returned by the ORDR API. */
export interface OrdrDeviceData {
  // Identity
  IpAddress?:          string;
  MacAddress?:         string;
  deviceName?:         string;
  fqdn?:               string;
  dhcpHostname?:       string;
  SerialNo?:           string;
  // Classification
  DeviceType?:         string;
  DeviceDescr?:        string;
  Group?:              string;
  Profile?:            string;
  endpointType?:       string;
  classificationState?:string;
  criticality?:        string;
  fdaClass?:           number;
  secondaryDevice?:    boolean;
  guestDevice?:        boolean;
  ou?:                 string;
  // Hardware / Software
  LongMfgName?:        string;
  MfgName?:            string;
  ModelNameNo?:        string;
  OsType?:             string;
  OsVersion?:          string;
  SwVersion?:          string;
  // Network
  Subnet?:             string;
  Vlan?:               number;
  vlanName?:           string;
  accessType?:         string;
  essid?:              string;
  dhcpEnabled?:        boolean;
  // Risk & Security
  RiskState?:          string;
  riskScore?:          number;
  knownVulnRiskState?: string;
  alarmCount?:         number;
  hasPhi?:             boolean;
  hasExternalFlows?:   string;
  isBlacklisted?:      boolean;
  proxied?:            boolean;
  // Status
  connStatus?:         string;
  firstSeen?:          string;
  lastSeen?:           string;
  // Network equipment
  nwEquipHostname?:    string;
  nwEquipInterface?:   string;
  nwEquipScrapeIp?:    string;
  // Location
  deviceLocation?:     string;
  sensorLocation?:     string;
  // Sensor
  sensorName?:         string;
  sensorIp?:           string;
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
