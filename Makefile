# rdsclock — passive RDS Clock-Time decoder
#
# Common targets:
#   make setup        — create .venv and install dependencies
#   make test         — run all tests
#   make test-fast    — skip tests marked "slow"
#   make coverage     — generate a coverage report
#   make demo         — synthetic 3-station multi-channel showcase (no SDR)
#   make recon        — live passive time receiver (requires SDR)
#   make recon-offline — passive recon replayed over eter/
#   make generate     — synthesize an IQ file with the current time
#   make demo-decode  — decode the freshly generated IQ (via `make generate`)
#   make live         — record 10s from RTL-SDR and decode (95.5 MHz)
#   make multi        — multi-station live demo (3 stations)
#   make scan         — sweep FM 87.5-108 MHz and find stations with CT
#   make clean        — remove .venv, pytest artefacts and synthetic IQ
#   make lint         — py_compile sanity check (no ruff/mypy required)

PY := .venv/bin/python
PIP := .venv/bin/pip
PYTEST := .venv/bin/pytest

SYNTH_IQ := build/synthetic.iq
SCAN_FREQ ?= 95.5
LIVE_DURATION ?= 10
MULTI_FREQS ?= 95.5,96.5,97.7

.PHONY: setup test test-fast coverage generate demo demo-decode live multi scan recon recon-offline play plot clean lint help

help:
	@echo "rdsclock — common commands:"
	@echo "  make setup          — create venv + install dependencies"
	@echo "  make test           — run pytest"
	@echo "  make test-fast      — skip slow tests"
	@echo "  make demo           — synthetic 3-station multi-channel showcase (no SDR)"
	@echo "  make recon-offline  — passive recon replayed over eter/"
	@echo "  make recon          — passive RDS time receiver LIVE (requires SDR)"
	@echo "  make play           — play mono FM audio live ($(SCAN_FREQ) MHz, audio optional dep)"
	@echo "  make plot           — render MPX spectrum PNG from $(SYNTH_IQ) (plot optional dep)"
	@echo "  make generate       — synthesize $(SYNTH_IQ) with the current time"
	@echo "  make demo-decode    — decode $(SYNTH_IQ)"
	@echo "  make live           — record $(LIVE_DURATION)s from $(SCAN_FREQ) MHz"
	@echo "  make multi          — multi-station live demo ($(MULTI_FREQS))"
	@echo "  make scan           — sweep the FM band"
	@echo "  make clean          — clean artefacts"

.venv/bin/python:
	python3 -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[audio,plot,dev]"
	@echo "venv ready: $$($(PY) --version)"

setup: .venv/bin/python

test: setup
	$(PYTEST) tests/

test-fast: setup
	$(PYTEST) tests/ -m "not slow and not real_sdr"

coverage: setup
	$(PYTEST) tests/ --cov=rdsclock --cov-report=term-missing --cov-report=html --cov-report=xml
	@echo "HTML: htmlcov/index.html"
	@echo "XML:  coverage.xml (consumed by SonarCloud)"

build:
	mkdir -p build

demo: setup
	$(PY) -m rdsclock demo

generate: setup build
	$(PY) -m rdsclock generate $(SYNTH_IQ) --duration 3.0 --snr 25 --ps "RELEASE "
	@ls -la $(SYNTH_IQ)

demo-decode: setup
	@test -f $(SYNTH_IQ) || (echo "Run \`make generate\` first."; exit 1)
	$(PY) -m rdsclock decode $(SYNTH_IQ)

live: setup
	$(PY) -m rdsclock live --freq $(SCAN_FREQ) --duration $(LIVE_DURATION)

multi: setup
	$(PY) -m rdsclock multi --freqs $(MULTI_FREQS) --duration 8

scan: setup
	$(PY) -m rdsclock scan --duration 3

recon: setup
	$(PY) -m rdsclock recon \
		--start 87.5 --end 108 --step 0.5 \
		--scan-dwell 1.5 --dwell 8 --idle 2 \
		--rescan-min 10 --max-stations 5 \
		--rssi-threshold -25 --gain 35

recon-offline: setup
	$(PY) -m rdsclock recon --from-dir eter

play: setup
	$(PY) -m rdsclock play --freq $(SCAN_FREQ)

plot: setup
	@test -f $(SYNTH_IQ) || (echo "Run \`make generate\` first."; exit 1)
	$(PY) -m rdsclock plot $(SYNTH_IQ) --out build/synthetic_mpx.png
	@ls -la build/synthetic_mpx.png

lint: setup
	$(PY) -m compileall -q src/rdsclock tests
	@echo "compileall OK"

clean:
	rm -rf .venv build htmlcov .pytest_cache .coverage *.egg-info src/*.egg-info
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned."
