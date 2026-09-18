# Phase 1 logic engine

This folder contains an isolated dictionary and word-sense layer. It does not
open frames, store answers, or call an LLM.

## Run the demo

```bash
python -m src.logic_engine.demo
```

The analyzer emits every word plus recognized groups (names, dates, money, and
basic street addresses). Groups overlap their component words deliberately, so
no word is skipped. Positions are zero-based, half-open character offsets.

## Dictionary behavior

The engine uses NLTK WordNet when the package and corpus are available. Install
the optional dependency and corpus with:

```bash
pip install nltk
python -m nltk.downloader wordnet
```

A small offline fallback keeps the demo deterministic before WordNet is
installed. Legal terms use the local legal dictionary first. Pattern groups use
simple deterministic rules. The LLM selector is an explicit no-op stub and
makes no network calls.
