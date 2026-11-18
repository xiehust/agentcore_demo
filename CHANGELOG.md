# Changelog

All notable changes to this repository are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `29-gateway-interceptor-search-routing`: AnySearch as the default Chinese search route (key in AgentCore Identity, `Bearer` header injected by the Gateway, Markdown results normalized); `deploy.py --cn-provider mock|exa|anysearch` selects the route.
- `29-gateway-interceptor-search-routing`: AgentCore Gateway interceptor that routes web search by query language (English to AgentCore Web Search, Chinese to Exa), exposes a single search tool and normalizes both result formats.
- `25-patch-manager-china`: Patch Manager on-call agent on AgentCore (China regions).
- `26-runtime-dsh`: cold-start and first-message benchmark for a DeepSeek Harness (`dsh headless`) agent on AgentCore Runtime platform V2 (snapshot restore), with a page-pin optimization (sample code included) and a local Docker baseline.
- `CONTRIBUTING.md` with a how-to-build section.
- FAQ section and directory index in `README.md`.

### Changed

- `README.md` is now written in Chinese.
