# Changelog

## 0.2.0

- Discover N3 HV inverter state, battery and BMS data, connection status,
  temperature, inverter AC phase, and EPS sensors in Home Assistant.
- Report fault codes with raw word attributes, show unrecognized states as
  `Unknown`, and use inverter state to determine whether a fault is active.
- Add a debounced bypass relay configuration switch with confirmed writes,
  disabled by default.
- Poll inverter temperatures, BMS cell data, and fault words separately from
  live measurements, and use that interval to retry failing telemetry blocks.
