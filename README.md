# OKX Live Trader Plugin

A skill-focused plugin that packages the `okx-live-trader` workflow into an importable plugin structure.

## Contents

- `.claude-plugin/plugin.json` — standalone plugin manifest
- `skills/okx-live-trader/SKILL.md` — primary workflow
- `commands/okx-live-trader.md` — optional slash-command compatibility entry

## What it does

When invoked, the workflow asks ChatGPT to use available OKX live market-data capabilities to:

1. scan gainers/losers;
2. filter low-quality or extreme movers;
3. compare a candidate against BTC/ETH/SOL;
4. inspect a price chart;
5. produce a conditional short-term trading plan;
6. clearly distinguish analysis from real order execution.

## Important dependency note

This package intentionally does not declare an `.app.json` dependency because no public OKX app ID was discoverable in the current ChatGPT Plugin Directory. It therefore relies on OKX market-data tools already being available in the ChatGPT environment where the plugin runs.

If a formal OKX ChatGPT app ID becomes available, add a root-level `.app.json` referencing that app and wire it into a native plugin manifest as documented by OpenAI.

## Import path

For managed ChatGPT workspaces, place this folder in a GitHub repository and import it via:

Workspace settings → Plugins → Add → Import marketplace

A standalone repository containing `.claude-plugin/plugin.json` is a supported import format. After import, set the plugin installation policy to Available or Installed as appropriate.

Once installed on a supported ChatGPT surface, invoke the plugin with an `@` mention, for example:

`@okx-live-trader scan the market and give me the strongest liquid momentum setup.`

Slash-command behavior depends on the client/surface; the `commands/` file is included for compatibility but is not required for ChatGPT `@` invocation.
