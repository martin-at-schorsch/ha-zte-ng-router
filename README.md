# ZTE NG Router – Home Assistant Custom Integration

Custom Home Assistant integration to monitor and control **modern ZTE “NG” 5G/FWA routers** via their **local HTTP API**.

The integration exposes key radio, network, and traffic metrics as Home Assistant entities and, where supported, provides basic control functions for use in dashboards and automations.


## Supported ZTE Router Models

This integration targets **recent ZTE NG router platforms** with a shared firmware and API structure.

### Supported models
- **ZTE G5TC** – 5G FWA / Indoor CPE  
- **ZTE G5TS** – 5G Indoor CPE (Wi-Fi 6)  
- **ZTE G5C**  
- **ZTE G5 Max**  
- **ZTE G5 Ultra**

> ⚠️ Support depends on **firmware version and operator customizations**. ISP-branded devices may restrict or disable parts of the local HTTP API.

If you successfully use this integration with a listed model and specific firmware, consider contributing that information via an issue or pull request.



## Features

Feature availability depends on router model and firmware.

- Connection status and uptime  
- Access technology (4G / 5G / NR)  
- Radio signal metrics (RSRP, RSRQ, SINR, PCI, bands)  
- WAN / external IP information  
- Traffic counters and current throughput  
- Device information (model, firmware, IMEI/ICCID where available)  
- Optional controls (e.g. reboot, mobile data on/off)

If a feature is missing, please open an issue to report it.

## Requirements

- Local network connectivity between Home Assistant and the router IP
