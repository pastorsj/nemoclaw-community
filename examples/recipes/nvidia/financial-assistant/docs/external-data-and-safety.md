<!--
  SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
  SPDX-License-Identifier: Apache-2.0
-->

# External Data and Safety

The Financial Assistant uses public market and filing data for educational
research. Public access does not remove a provider's terms, attribution,
rate-limit, or redistribution requirements. The operator is responsible for
confirming that each source and inference provider is allowed for the intended
use.

## Data sources

| Source | Recipe use | Important boundary |
|---|---|---|
| Yahoo Finance chart endpoint at `query1.finance.yahoo.com` | Small, read-only quote snapshots requested by ticker | Data can be delayed and carries provider and exchange terms. It is not a licensed trading feed. |
| SEC ticker map at `www.sec.gov` | Resolve a public-company ticker to its Central Index Key (CIK) | Follow SEC fair-access guidance and identify automated requests. |
| SEC company facts at `data.sec.gov` | Retrieve selected public XBRL company facts | A fact record does not replace the complete filing or its notes. |
| NVIDIA inference endpoint configured by `NEMOCLAW_ENDPOINT_URL` | Process the request, tool results, and assistant context | Policy permits only the documented NVIDIA endpoint. Its logging, retention, model behavior, and data policy are outside this recipe. |

Review the current source rules before use:

- [Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)
- [Yahoo Finance market coverage, providers, delays, and redistribution notice](https://help.yahoo.com/kb/finance/article-exchanges-data-delays-sln2310.html)
- [SEC developer resources and fair-access guidance](https://www.sec.gov/about/developer-resources)
- [SEC guidance for accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

Yahoo states that its Finance information is for informational purposes and
restricts redistribution. Use the bundled snapshot helper for private
evaluation, not to republish or resell Yahoo data. If the intended deployment
needs redistribution, guaranteed latency, service commitments, or trading
use, replace that helper with an approved licensed provider and update the
policy, notices, tests, and documentation.

SEC guidance currently limits automated access to no more than 10 requests per
second across a user's machines. This recipe is designed for small ticker
lists, not bulk crawling. Set a descriptive value in `.env` before bring-up:

```dotenv
SEC_USER_AGENT="Your Name your.email@example.com"
```

The user-agent value is sent to the SEC. Use a contact address that is
appropriate for that disclosure. The SEC can block excessive or unidentified
automation.

## Financial-use boundary

The assistant can summarize sourced public facts and label hypotheses. It
cannot establish that a security, strategy, or transaction is appropriate for
an individual or organization.

- Do not use the output as personalized investment, legal, accounting, or tax
  advice.
- Do not use quote snapshots as an execution price or real-time feed.
- Do not ask the assistant to place an order or connect it to a brokerage or
  portfolio account.
- Do not provide material nonpublic information, account data, credentials, or
  other regulated personal data.
- Confirm dates, units, fiscal periods, amendments, and filing context in the
  original SEC filing.
- Treat model conclusions as hypotheses until a qualified person checks the
  cited facts and assumptions.

The assistant can still produce an incorrect interpretation when the source
data is correct. A citation or trace proves what data was processed; it does
not prove that the answer is complete or suitable.

## Network and credential boundary

OpenShell policy allows the configured NVIDIA inference route, the two SEC
hosts, the Yahoo Finance chart host, and `POST /v1/traces` to the local Phoenix
collector. It does not allow Microsoft Graph, email, brokerages, portfolio
services, arbitrary web browsing, or remote object storage.

`NVIDIA_API_KEY` remains in the OpenShell provider store. The Yahoo and SEC
helpers require no secret. `SEC_USER_AGENT` is caller identification, not a
secret. Do not place an API key, account identifier, private endpoint, or
personal financial record in that value. Optional `GITHUB_TOKEN` is passed to
Docker only as a build secret when the pinned Hermes archive needs
authenticated download; it is not a sandbox runtime credential.

## Prompt, trace, and retention boundary

Inference requests can include the operator's prompt, conversation context,
public tool results, and generated answer. Confirm that the configured NVIDIA
inference endpoint's data policy meets the deployment's requirements.

Native NeMo Relay records agent, model, and tool activity. It writes or
refreshes local ATIF after a completed Hermes turn and closes the Relay session
on finalize or reset. Local ATIF files and Phoenix can therefore contain:

- prompts and generated answers;
- requested ticker symbols and research assumptions;
- public quote and SEC tool results;
- errors, timestamps, model identifiers, and tool arguments.

Loopback binding prevents direct network access from another host; it does not
encrypt data at rest or isolate users who already control the same host. Use a
single trusted operator per deployment. Download ATIF only when needed, protect
the archive as research content, and delete ATIF and Phoenix state according to
an explicit retention policy. `tear-down.sh --purge-host-services` deletes the
local `phoenix-data` Docker volume but not `.traces/` archives or verification
output in `.tmp/`. This recipe does not send traces to remote object storage.

## Provenance and third-party responsibilities

The finance and SEC skill core originated in Sam Pastoriza's public
`feature/hermes-financial-assistant` contribution and was reconstructed as an
NVIDIA-maintained recipe for the current NemoClaw Community taxonomy and
runtime. No Yahoo, SEC, Microsoft, brokerage, or exchange logo or data fixture
is distributed with this recipe.

Yahoo, SEC EDGAR, the inference provider, Hermes, NemoClaw, OpenShell, NeMo
Relay, and Phoenix retain their own terms and support boundaries. Review the
repository's root [`THIRD-PARTY-NOTICES`](../../../../../THIRD-PARTY-NOTICES) and
the linked provider terms before distributing a derived deployment.
