# References

Public sources used to build and verify `rdsclock`'s RDS decoder.

## Primary Standards

### IEC 62106-2:2021 — *Radio Data System (RDS) — Part 2: Message format*

The canonical modern reference for RDS. Defines the block-CRC
syndromes (offset words A / B / C / C' / D), the group-type
catalogue, and the Clock-Time (Group 4A) bit layout that
`rdsclock` implements bit-for-bit (Figure 11). Successor to the
older ETSI EN 62106:2009.

- Source: <https://webstore.iec.ch/publication/61173>
- Status: paid (≈ CHF 250).

### NRSC-4-B — *United States RBDS Standard*

The US "Radio Broadcast Data System" standard. RBDS is a superset
of IEC 62106 — it adds North-American-specific groups for program
type names and traffic alerts — but the block structure, CRC offset
words, sync codes and the Group 4A clock-time layout are identical.
NRSC-4-B is therefore a **free public substitute** for the IEC
document when only Clock-Time decoding matters.

- Source: <https://nrscstandards.org/standards-and-guidelines/>
- Status: free PDF download.

## Open-Source Implementations

### redsea — Oona Räisänen

The de-facto reference open-source RDS decoder in C++. Excellent
reading for anyone trying to understand how RDS demodulation works
in practice — symbol-clock recovery, block sync, CRC repair, group
dispatch. `rdsclock` does **not** vendor any redsea code; its DSP
path is a clean Python re-implementation from the standard. The
redsea source and accompanying notes were a useful secondary
reference during development.

- Source: <https://github.com/windytan/redsea>
- Licence: MIT.

## Background Reading

### Wikipedia — Radio Data System

A surprisingly thorough overview that links to the historical specs
(CENELEC EN 50067, ETSI EN 62106) and explains the practical
deployment of PI codes, PTY tables and clock-time.

- <https://en.wikipedia.org/wiki/Radio_Data_System>

## Related Hardware and Protocols

- **RTL-SDR Blog v4** — the inexpensive DVB-T dongle that this
  package treats as its FM receiver. Drivers, firmware notes and
  community documentation at <https://www.rtl-sdr.com/>.
- **rtl_tcp** — the line-oriented byte-streaming protocol that
  `rdsclock` speaks to receive samples from the dongle. Part of
  the osmocom rtl-sdr project at <https://osmocom.org/projects/rtl-sdr>.
