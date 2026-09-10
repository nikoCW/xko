# Minimal patch to the existing xko `server.py`

The current `server.py` already creates:

```python
mcp = MCPServer(...)
```

For the first integration test, copy these files next to `server.py`:

```text
nautilus_mcp_tools.py
trading_models.py
```

Then add:

```python
from nautilus_mcp_tools import register_nautilus_tools

register_nautilus_tools(mcp)
```

Keep the existing read-only market tools unchanged.

Do **not** enable order submission in the deployed public Render service yet. Run the Nautilus bridge separately with OKX Demo credentials first and keep `ALLOW_ORDER_SUBMIT=false` until the protective stop/TP lifecycle is implemented and tested.
