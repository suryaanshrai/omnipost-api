"""Provider-agnostic AI layer: text/image generation across a managed default
provider and BYOK alternatives, gated by per-workspace quota. See gateway.py
for the entry point most callers want (service.py uses it; nothing outside
this package should call providers.py directly)."""
