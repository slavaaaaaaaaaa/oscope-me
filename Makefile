VENV := .venv
PYTHON := $(VENV)/bin/python

.PHONY: run play test

run:  ## Start the app (waits for SDR, prompts for frequency)
	$(PYTHON) -m oscope_me $(ARGS)

play:  ## Play a file as X/Y music: make play FILE=song.flac
	$(PYTHON) -m oscope_me -i "$(FILE)" $(ARGS)

test:  ## Run the test suite (needs: pip install -e .[test])
	$(PYTHON) -m pytest tests/ -v
