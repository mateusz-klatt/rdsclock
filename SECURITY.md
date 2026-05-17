# Security Policy

## Project Threat Profile

`rdsclock` is a **passive RF receiver** for FM RDS Clock Time decoding.
It is designed for GPS-denied environments where an operator needs a
non-GPS, non-NTP source of UTC. The project is intentionally
**receive-only** — it never transmits, never emits identifiable RF.

For a detailed threat model, see [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).

## Supported Versions

Pre-1.0 releases: only the latest minor version receives security fixes.

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

Please **do not** open public issues for security-relevant findings.

Report security issues privately to: **mateusz@klatt.ie**

Acknowledgement: within 72 hours.
Initial assessment: within 7 days.
Fix or mitigation plan: within 30 days for high-severity issues.

## Hardening Notes for Operators

1. **rtl_tcp must bind to localhost only** in default deployment.
   Never expose `rtl_tcp -a 0.0.0.0` without a firewall, VPN, or SSH tunnel.
   The protocol has no authentication and no encryption.

2. **Recorded IQ files may reveal operational metadata**:
   capture frequency, capture time, geographic context via received
   stations. Treat IQ recordings, scan logs and `night_log.txt` as
   sensitive — sanitize before sharing.

3. **The receiver itself never identifies itself** to outside parties.
   No outbound network connections except local `rtl_tcp`.

4. **Anti-spoofing**: the consensus engine flags stations whose CT
   diverges from the median by more than `OUTLIER_THRESHOLD_S` (90s default).
   A single station should never be trusted as authoritative; multi-source
   consensus is mandatory.

5. **Holdover**: when all stations stop transmitting CT, the receiver
   continues estimating UTC from a local monotonic clock disciplined by
   an estimated ppm drift. Uncertainty grows linearly. The operator must
   monitor the displayed `±X` value.

## Legal Notes for Operators (Poland)

This software performs **passive radio reception**. Receiving public
FM broadcasts is lawful in Poland and across the EU. Two further
constraints bind operators in Poland and most EU jurisdictions:

- **Confidentiality of electronic communications** (*tajemnica
  komunikacji elektronicznej*) under the Electronic Communications Law
  (Dz.U. 2024 poz. 1221) prevents **disclosure and dissemination** of
  received content beyond the operator's own use.
- **Audio content** carried inside an FM IQ capture is a copyrighted
  work; redistribution requires the rights holders' permission.

In practice this means: local recording for technical or amateur-radio
use is widely accepted, but public redistribution of the recordings
typically requires permission from the rights holders. The synthetic
generator (`rdsclock.synth.synthesize_fm_iq`) produces copyright-free
IQ files that are safe to share for technical purposes. See
[`docs/datasets.md`](docs/datasets.md) for the longer discussion.
**Nothing here is legal advice.**
