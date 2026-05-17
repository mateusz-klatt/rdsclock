# Reference Datasets — and Why They Are Not in the Repository

`rdsclock` ships **no bundled IQ recordings**. This is a deliberate
legal and policy decision, not a storage concern.

## Legal Framing (Poland)

The primary reason the captures are kept out of the source tree is
**Polish electronic-communications law and copyright law**, not file
size. The relevant rules:

1. **Reception is lawful.** Anyone may receive publicly broadcast
   transmissions (such as FM radio) using a passive receiver such as
   an RTL-SDR. This follows from the general structure of the
   *Prawo komunikacji elektronicznej* (Electronic Communications Law,
   Dz.U. 2024 poz. 1221, in force since 10 November 2024), which
   replaces the earlier *Prawo telekomunikacyjne*.
2. **Personal recording is generally tolerated** for one's own
   technical or amateur-radio use (see the long-standing custom
   reflected in PZK — *Polski Związek Krótkofalowców* — operator
   guides and the SWL listening-station practice).
3. **Publishing or otherwise disseminating the captured content is
   restricted.** The Electronic Communications Law preserves the
   *tajemnica komunikacji elektronicznej* (confidentiality of
   electronic communications), and the audio content embedded in any
   FM IQ capture — music, speech, jingles, station identifications —
   is a copyrighted work under the *Ustawa o prawie autorskim
   i prawach pokrewnych*. Distributing recordings that contain such
   content requires permission from the rights holders, which we do
   not have for ad-hoc local captures.

Therefore any local `eter/` set is treated as **private research
data**, equivalent to a krótkofalowiec's reception log. Anyone
reproducing the experiments should make their own captures.

## How to Reproduce the Test Set Locally

```bash
# 31 sequential 60-second captures of the FM band
for f in 87.8 88.4 89.0 89.8 90.6 91.0 92.0 92.4 92.8 93.3 94.0 \
         94.7 95.8 96.5 97.1 97.7 98.3 98.8 100.1 101.0 101.5 \
         102.0 102.4 103.0 103.7 104.4 104.9 105.6 106.2 106.8 107.5; do
    rdsclock live --freq "$f" --duration 60 \
        --save "eter/fm_${f}_MHz.iq" --gain 30
done
```

Or use `scripts/night_recorder.py` for an overnight diurnal study
(writes into `scripts/night/`).

The synthetic generator (`rdsclock.synth.synthesize_fm_iq`) produces
copyright-free IQ files suitable for tests and demos. All
`tests/test_*` except `test_real_recordings.py` work on synthetic data
only and therefore run on CI and on any clean checkout.

## `eter/` Layout

If you do choose to record a local equivalent, the integration tests
(`tests/test_real_recordings.py`, marker `real_sdr`) expect:

- One `*.iq` file per station, 60 seconds, `complex64` at 250 kS/s.
- Filename pattern: `fm_<FFF.FF>_MHz.iq`
  (for example `fm_092.00_MHz.iq` for 92.0 MHz).

## `scripts/night/` — overnight bench captures

`scripts/night_recorder.py` writes a fresh capture every 30 minutes
into `scripts/night/`. Treat the resulting `.iq` files as sensitive
local data — see [`SECURITY.md`](../SECURITY.md) and the
[Threat Model](THREAT_MODEL.md) for handling guidance.

## References

- *Ustawa z dnia 12 lipca 2024 r. – Prawo komunikacji elektronicznej*
  (Dz.U. 2024 poz. 1221).
- *Ustawa o prawie autorskim i prawach pokrewnych*
  (Dz.U. 1994 nr 24 poz. 83 z późn. zm.).
- *Polski Związek Krótkofalowców* — guides for short-wave listeners
  (SWL) and licensed operators describe the customary handling of
  reception logs and QSL confirmations.
- For non-Polish jurisdictions, consult the local equivalent: most
  EU member states have similar confidentiality-of-communications
  provisions implementing Directive 2002/58/EC (ePrivacy).
