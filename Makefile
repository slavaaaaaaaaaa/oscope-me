VENV := .venv
PYTHON := $(VENV)/bin/python

ALSA_CARD ?= 0

.PHONY: venv setup-dual-analog run play test

venv:
	$(PYTHON) -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e .
	pulseaudio --start

setup-dual-analog:  ## Linux: disable ALSA auto-mute so jack + speakers play together
	@if [ "$$(uname -s)" = Linux ]; then \
	  amixer -c $(ALSA_CARD) sset 'Auto-Mute Mode' Disabled >/dev/null 2>&1 || true; \
	  amixer -c $(ALSA_CARD) sset Speaker unmute >/dev/null 2>&1 || true; \
	fi

run: setup-dual-analog  ## Start the app (waits for SDR, prompts for frequency)
	$(PYTHON) -m oscope_me $(ARGS)

play: setup-dual-analog  ## Play a file as X/Y music: make play FILE=song.flac
	$(PYTHON) -m oscope_me -i "$(FILE)" $(ARGS)

test:  ## Run the test suite (needs: pip install -e .[test])
	$(PYTHON) -m pytest tests/ -v
