# ipybox-sandbox

Stateful IPython kernel sandbox with pluggable extensions.

## Installation

```bash
pip install -e .
```

## Usage

```bash
ipybox-server --host 0.0.0.0 --port 9006
```

## Extensions

Extensions are discovered from `IPYBOX_EXTENSIONS_DIR` (default `/opt/ipybox/extensions`).

## Docker

```bash
docker build -t ipybox-sandbox .
docker run -p 9006:9006 ipybox-sandbox
```
