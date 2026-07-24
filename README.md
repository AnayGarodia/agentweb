# AgentWeb

**Let coding agents *use* websites — read data and take real actions — without opening a browser.**

AgentWeb turns a website into simple, typed commands that return clean JSON. There
are two kinds:

**Read** — look things up:

```bash
agentweb arxiv.org search-papers --query "graph neural networks" --limit 3
agentweb npmjs.com get-version --package react --version latest
agentweb wikipedia.org page --title "Alan Turing"
```

**Act** — actually do things on your own accounts. Impactful or irreversible
actions (checkout, posting, opening a PR) require an explicit `--confirm`:

```bash
agentweb spotify play --query "Massive Attack Teardrop"
agentweb amazon add-to-cart --asin B0XXXXXXXX
agentweb github create-pull-request --owner you --repo app --head fix --base main --title "Fix the thing" --confirm
```

An agent discovers what a site supports, calls the right command, and uses the
result directly — no screenshots, no hunting for buttons, no re-parsing a page for
a workflow that has already been mapped.

**Actions are the point.** Reads are often a web search away; *doing* things — play
a song, place an order, open a pull request, post a comment — is what a browserless
interface uniquely unlocks. Every mapped action is typed, state-changing ones are
confirmation-gated, and each is verified against the live site where possible;
where a site offers no safe way to prove an action, AgentWeb says so instead of
pretending.

## Try it in one minute

AgentWeb supports macOS and Linux. If Python 3.11 or newer is not already
available, the installer provisions an isolated Python automatically.

```bash
curl -fsSL https://github.com/AnayGarodia/agentweb/raw/refs/heads/main/install.sh | sh
```

Restart Claude Code or Codex, then paste this prompt:

```text
Use AgentWeb to find the latest version of React on npm.
```

That is the intended experience. The installer adds a global AgentWeb skill for
Claude Code and Codex containing the CLI's absolute path, and installs everything
in an isolated environment under `~/.local/share/agentweb`. New agent sessions can
therefore discover AgentWeb even when the app does not inherit `~/.local/bin` in its
PATH. It does not change your system Python packages or register MCP automatically.

Prefer to inspect scripts before running them? [Read `install.sh`](install.sh), or
install from a checkout:

```bash
git clone https://github.com/AnayGarodia/agentweb.git
cd agentweb
python3 -m pip install -e .
agentweb setup
```

### Upgrading an existing install

Already installed? Upgrade in place with one command — it upgrades the exact
launcher on your `PATH` (no second, drifting copy) and preserves your
`~/.agentweb` sessions and profiles:

```bash
agentweb upgrade          # checksum-verifies and installs the latest release
agentweb upgrade --check  # just report installed vs. latest; change nothing
```

`agentweb upgrade` re-runs the official installer after verifying it against its
published `install.sh.sha256`. Re-running the one-line installer from
[Try it in one minute](#try-it-in-one-minute) does the same thing.

## Just ask for what you want

Once installed, prompts can be ordinary tasks:

```text
Use AgentWeb to find three recent arXiv papers about coding agents.

Use AgentWeb to compare the dependencies of React and Vue on npm.

Use AgentWeb to research Alan Turing on Wikipedia and follow the most relevant links.

Use AgentWeb to find software engineering jobs on LinkedIn in San Francisco.
```

The agent handles discovery and commands. You should not need to translate your
request into CLI syntax. If the installer could not detect your coding agent, the
manual connection commands are:

```bash
agentweb install-agent claude --scope user
agentweb install-agent codex --scope user
```

See [agent setup](docs/AGENT_HOSTS.md) for the exact behavior Claude Code and Codex
should follow around login, retries, and writes.

## See whether AgentWeb is being used

Open the private usage dashboard:

```bash
agentweb dashboard
```

It opens on `127.0.0.1` and immediately shows activity from this installation:
people, first-task activation, repeat use, website actions, success rate, latency,
common operations, agent paths, and failures. Connect a PostHog project from the
same page when you want the localhost dashboard to combine anonymous events from
public installations.

AgentWeb never records prompts, arguments, URLs, website responses, account
identities, cookies, or credentials. Inspect or disable analytics at any time:

```bash
agentweb telemetry inspect
agentweb telemetry disable
```

Read [usage analytics](docs/ANALYTICS.md) for the exact event schema and global
dashboard setup.

## What the agent does underneath

```bash
# See which websites are installed
agentweb sites

# Discover what one website supports
agentweb capabilities arxiv.org

# Run the selected action
agentweb arxiv.org search-papers --query "attention" --limit 3
```

Most users do not need to run these themselves. They are the predictable interface
the coding agent uses. If an argument is unclear, the agent can inspect only that
action:

```bash
agentweb describe arxiv.org --operation search_papers
```

Commands accept normal domains and many normal website URLs. A command's main
argument can be given either positionally or as a flag — `agentweb wikipedia.org
page "Alan Turing"` and `agentweb wikipedia.org page --title "Alan Turing"` are
equivalent — so copy-pasted examples work either way. Results and expected errors
are JSON, so agents do not need site-specific parsing code.

## Supported sites and endpoints

The hosted registry currently serves **26 sites** with **306 typed operations**. Every install follows the hosted registry by default, so newly published adapters appear on the next `agentweb sync` (which also runs automatically) without upgrading the tool. Run `agentweb sites` to see what is installed and `agentweb capabilities DOMAIN` for a site's live operation list.

Verification status per operation:

- **verified** — passed the release gate's live semantic verification: the operation was replayed against the real site and its response content checked against reviewed assertions.
- **shape-verified** — a live replay confirmed the response structure matches the reviewed schema, but content-level assertions were not part of the evidence.
- **unverified** — mapped and schema-typed, but no machine-checked live evidence is bundled; these are older adapters that were verified manually during QA.

| Site | Domain | Version | Operations | Verified | Login |
| --- | --- | --- | ---: | --- | --- |
| Amazon | `amazon.com` | 0.9.24 | 23 | 10/23 shape-verified | for some operations |
| arXiv | `arxiv.org` | 0.1.5 | 13 | all verified | none |
| BSE | `bseindia.com` | 0.2.0 | 4 | all verified | none |
| CoinGecko | `coingecko.com` | 0.1.0 | 2 | all verified | none |
| Crossref | `crossref.org` | 0.2.0 | 3 | all verified | none |
| Federal Register | `federalregister.gov` | 0.1.0 | 2 | all verified | none |
| GitHub | `github.com` | 0.9.12 | 33 | 5/33 shape-verified | for some operations |
| GST | `gst.gov.in` | 0.1.2 | 12 | all verified | none |
| Hacker News | `news.ycombinator.com` | 0.8.9 | 17 | 4/17 shape-verified | for some operations |
| Hugging Face | `huggingface.co` | 0.1.1 | 53 | all verified | none |
| IBBI | `ibbi.gov.in` | 0.2.0 | 2 | all verified | none |
| India Post | `postalpincode.in` | 0.1.0 | 2 | all verified | none |
| Nominatim (OpenStreetMap) | `openstreetmap.org` | 0.1.0 | 2 | all verified | none |
| npm | `npmjs.com` | 0.1.1 | 17 | all verified | none |
| NSE | `nseindia.com` | 0.2.0 | 3 | all verified | none |
| Open Brewery DB | `openbrewerydb.org` | 0.1.1 | 5 | all verified | none |
| Open Library | `openlibrary.org` | 0.1.0 | 3 | all verified | none |
| Open-Meteo | `open-meteo.com` | 0.2.0 | 4 | all verified | none |
| PyPI | `pypi.org` | 0.1.0 | 18 | all verified | none |
| RBI | `rbi.org.in` | 0.2.0 | 3 | all verified | none |
| SEBI | `sebi.gov.in` | 0.1.0 | 1 | all verified | none |
| SEC EDGAR | `sec.gov` | 0.1.0 | 3 | all verified | none |
| Spotify | `open.spotify.com` | 0.4.8 | 38 | 20/38 shape-verified | for some operations |
| Stack Overflow | `stackoverflow.com` | 0.8.7 | 22 | unverified (manual QA) | for some operations |
| Wikipedia | `wikipedia.org` | 0.8.6 | 19 | 3/19 shape-verified | for some operations |
| World Bank | `worldbank.org` | 0.1.0 | 2 | all verified | none |

<details>
<summary><b>Amazon</b> (amazon.com) — 23 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `account_status` | Check whether the connected Amazon profile currently represents a signed-in account | shape-verified |
| `add_address` | Add and select a delivery address for the current Amazon checkout (write, `--confirm` required; login required) | unverified |
| `add_to_cart` | Fetch fresh product tokens and add an item (write, `--confirm` required) | shape-verified |
| `add_to_list` | Add a product to a specific Amazon List (wishlist) by ASIN and list_id, then confirm by reading the list back (write, `--confirm` required; login required) | unverified |
| `batch_products` | Fetch compact current title, price, sale, image, and addability data for up to 25 ASINs in two direct requests | shape-verified |
| `best_sellers` | Read Amazon's public best-seller list, optionally for a department slug | shape-verified |
| `cart` | Read the current profile cart, including whether it belongs to an account or is an isolated anonymous cart tha | shape-verified |
| `checkout` | Inspect the live account checkout by its active forms rather than generic links shared by every checkout page (write, `--confirm` required; login required) | unverified |
| `checkout_url` | Return Amazon's checkout URL | unverified |
| `compare_products` | Fetch and compare 2 to 10 products, including current price, rating, availability, seller, features, and sale  | unverified |
| `deals` | Search Amazon's Today's Deals index | shape-verified |
| `list` | Read one Amazon List and the product cards currently rendered on it (login required) | shape-verified |
| `lists` | List the signed-in account's Amazon Lists with IDs, privacy, default-list state, suspected QA artifacts, and a (login required) | shape-verified |
| `orders` | Read recent order cards using a signed-in imported session (login required) | unverified |
| `payment_methods` | List payment methods exposed by the current checkout without returning Amazon's opaque instrument tokens (login required) | unverified |
| `product` | Fetch current product, price, availability, rating, sale, seller, and cart metadata | shape-verified |
| `recommendations` | List related products directly linked from a public product page, excluding detected credit-card and account p | unverified |
| `remove_from_cart` | Remove an ASIN from the account cart by default, or from an explicitly selected isolated anonymous cart (write, `--confirm` required) | shape-verified |
| `remove_from_list` | Remove an item from a specific Amazon List (wishlist) by list_id and item_id, then confirm by reading the list (write, `--confirm` required; login required) | unverified |
| `reviews` | Read the public featured customer reviews visible on a product page | unverified |
| `sale_check` | Check whether a product currently displays a discount or lower price | unverified |
| `search` | Search Amazon products and return compact structured results | unverified |
| `variations` | List product option groups and the ASINs exposed for selectable variants | unverified |

</details>

<details>
<summary><b>arXiv</b> (arxiv.org) — 13 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `download_pdf` | Download the rendered PDF for a known public arXiv paper and return a local file receipt with an integrity dig | verified |
| `download_source` | Download the public source bundle for a known arXiv paper and return a local file receipt with an integrity di | verified |
| `get_bibtex` | Retrieve arXiv's BibTeX citation text for a known public paper identifier | verified |
| `get_category_feed` | Read and normalize the current public RSS announcements for an exact arXiv category | verified |
| `get_papers` | Retrieve normalized metadata for one or more exact arXiv identifiers, including version-specific records when  | verified |
| `list_author_papers` | Find public arXiv papers credited to a named author and return the newest matching records with normalized met | verified |
| `list_bulk_data_resources` | List official arXiv bulk-access resources for metadata, PDFs, source archives, terms, and incremental harvesti | verified |
| `list_categories` | List arXiv's complete public archive and subject-category catalog with canonical browsing links | verified |
| `list_category_papers` | List the newest public papers in an exact arXiv subject category with normalized metadata and stable offset pa | verified |
| `list_export_formats` | List the public format, source, help, and scholarly-resource links exposed by arXiv for a known paper | verified |
| `list_site_resources` | List official public arXiv information, help, policy, status, statistics, accessibility, and project-resource  | verified |
| `read_site_resource` | Read the normalized text of an official public arXiv information, help, policy, accessibility, statistics, or  | verified |
| `search_papers` | Search the complete public arXiv corpus with its expressive query language and return normalized paper metadat | verified |

</details>

<details>
<summary><b>BSE</b> (bseindia.com) — 4 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `announcements` | List a company's regulatory corporate announcements filed with BSE, newest first, with attachment metadata | verified |
| `corporate_actions` | List a BSE-listed company's corporate actions (dividends, splits, bonuses, schemes) with ex-dates and record d | verified |
| `dividends` | List a BSE-listed company's dividend history with record dates and per-share amounts | verified |
| `quote` | Read a listed company's identity, live price, and market header data from BSE by scrip code | verified |

</details>

<details>
<summary><b>CoinGecko</b> (coingecko.com) — 2 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `markets` | List coins ranked by market cap with live price, market cap, and 24h change in a chosen fiat currency | verified |
| `search` | Search CoinGecko for coins by name or symbol and return their CoinGecko ids and market cap ranks | verified |

</details>

<details>
<summary><b>Crossref</b> (crossref.org) — 3 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `search_journals` | Search Crossref's public journal directory and return matching journal titles and publishers | verified |
| `search_works` | Search Crossref's public scholarly metadata for works and return bounded matches with DOIs, titles, and citati | verified |
| `work` | Fetch one work's metadata by DOI | verified |

</details>

<details>
<summary><b>Federal Register</b> (federalregister.gov) — 2 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `document` | Fetch one Federal Register document's full metadata by document number | verified |
| `search` | Full-text search of Federal Register documents (US rules, proposed rules, notices, and presidential documents) | verified |

</details>

<details>
<summary><b>GitHub</b> (github.com) — 33 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `add_reaction` | Add an emoji reaction to an issue or pull request via the authenticated REST API (write, `--confirm` required; login required) | unverified |
| `api_get` | Bounded read-only escape hatch for public GitHub REST endpoints not yet represented by a typed command | unverified |
| `api_request` | Call any fixed-host GitHub REST or GraphQL endpoint (write, `--confirm` required) | unverified |
| `auth_status` | Report website-session and API-token authentication separately, including API quota tier and the age/freshness | unverified |
| `branches` | List public repository branches and protection state | shape-verified |
| `comment` | Add a comment to an issue or pull request via the authenticated REST API (write, `--confirm` required; login required) | unverified |
| `commits` | List recent public commits, optionally restricted to a branch, SHA, or file path | shape-verified |
| `configure_token` | Validate and save a GitHub fine-grained or personal access token in the local AgentWeb profile with mode 0600 (write, `--confirm` required) | unverified |
| `contents` | List a public repository directory or read a bounded UTF-8 file | unverified |
| `contributors` | List public repository contributors and contribution counts | unverified |
| `create_gist` | Create a gist for the authenticated user via the REST API (write, `--confirm` required; login required) | unverified |
| `create_issue` | Create an issue using the retained website login or a configured API token; either credential works (write, `--confirm` required; login required) | unverified |
| `create_pull_request` | Open a pull request through the authenticated GitHub REST API using a configured fine-grained token (write, `--confirm` required; login required) | unverified |
| `create_release` | Create a release (optionally draft or prerelease) for a repository via the authenticated REST API (write, `--confirm` required; login required) | unverified |
| `disconnect` | Delete the GitHub token stored in this AgentWeb profile (write, `--confirm` required) | unverified |
| `dispatch_workflow` | Trigger a workflow_dispatch run for a GitHub Actions workflow via the authenticated REST API (write, `--confirm` required; login required) | unverified |
| `fork_repository` | Fork a repository into the authenticated user's account via the REST API (write, `--confirm` required; login required) | unverified |
| `issue` | Fetch one public issue or pull-request-shaped issue with bounded body text | unverified |
| `issue_comments` | Read bounded public comments on an issue or pull request | unverified |
| `issues` | List public issues for a repository; pull requests are omitted explicitly | unverified |
| `merge_pull_request` | Merge an open pull request via the authenticated REST API (write, `--confirm` required; login required) | unverified |
| `pull_request` | Fetch one public pull request with merge state and change statistics | unverified |
| `pull_request_files` | List changed files and bounded patches for a public pull request | unverified |
| `pull_requests` | List public pull requests for a repository | unverified |
| `releases` | List public repository releases and bounded asset metadata | unverified |
| `repository` | Fetch public metadata for one GitHub repository | shape-verified |
| `review_pull_request` | Submit a pull request review (APPROVE, REQUEST_CHANGES, or COMMENT) via the authenticated REST API (write, `--confirm` required; login required) | unverified |
| `search_repositories` | Search public GitHub repositories with compact metadata and rate-limit status | unverified |
| `set_star` | Star or unstar a repository for the authenticated user via the REST API (write, `--confirm` required; login required) | unverified |
| `tags` | List public repository tags and archive links | shape-verified |
| `update_issue` | Edit an issue or pull request (title, body, state, or state_reason) via the authenticated REST API (write, `--confirm` required; login required) | unverified |
| `user` | Fetch one public GitHub user profile | unverified |
| `website_graphql` | Replay a persisted operation used by GitHub's current website (write, `--confirm` required; login required) | shape-verified |

</details>

<details>
<summary><b>GST</b> (gst.gov.in) — 12 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `download_hsn_sac_directory` | Download the GST portal's published HSN and SAC directory as a local Excel file with a verification receipt | verified |
| `list_advisories` | List current or archived GSTN advisories for a calendar year with clean text, dates, modules, and links | verified |
| `list_due_dates` | Return the GST portal's current filing, statement, refund, and quarterly compliance deadlines as structured da | verified |
| `list_gst_law_sources` | List every official central, state, and union-territory source linked by GSTN for GST laws and legal updates | verified |
| `list_gst_statistics` | List every official GST registration, return, collection, settlement, e-way-bill, and yearwise statistical dow | verified |
| `list_help_resources` | List every public GSTN help, support, grievance, system-requirement, and taxpayer-facility resource with its o | verified |
| `list_holidays` | List GST portal centre and state non-working days for one state code and calendar year | verified |
| `list_offline_tools` | List every current GST offline return, ITC, transition, advance-ruling, and IMS utility with its official URL | verified |
| `list_states` | List GST state and union territory codes, names, and territory classification for other state-scoped operation | verified |
| `search_gst_practitioners` | Search the public GST practitioner register by state, optional name, district, or PIN code, without exposing u | verified |
| `search_hsn_sac` | Look up live GST portal HSN or SAC classification suggestions from a known numeric code | verified |
| `search_hsn_sac_by_description` | Find live GST portal HSN goods or SAC services classification candidates from a plain-language description | verified |

</details>

<details>
<summary><b>Hacker News</b> (news.ycombinator.com) — 17 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `account_status` | Verify the retained Hacker News session and report the signed-in username | shape-verified |
| `api_get` | Bounded read-only escape hatch for official Hacker News Firebase or Algolia endpoints | unverified |
| `comment` | Post a comment or reply by extracting the current hidden HN form fields and submitting them directly (write, `--confirm` required; login required) | unverified |
| `delete` | Delete an eligible owned item via the delete-confirm form and verify the deletion by re-reading the item page (write, `--confirm` required; login required) | unverified |
| `edit` | Edit an eligible owned item through HN's current edit form (write, `--confirm` required; login required) | unverified |
| `favorite` | Favorite or unfavorite an item by extracting and replaying the current authenticated action URL (write, `--confirm` required; login required) | shape-verified |
| `flag` | Flag or unflag an eligible item through its current authenticated action link (write, `--confirm` required; login required) | unverified |
| `item` | Fetch one story or comment with live score and comment totals from official Firebase plus a bounded comment tr | unverified |
| `max_item` | Return the newest allocated Hacker News item ID | unverified |
| `search` | Search Hacker News stories and comments by relevance or date using compact text previews; call hn | unverified |
| `stories` | Read a current ranked Hacker News feed with compact story details | shape-verified |
| `story_comments` | List flat comments for one story by date or relevance | unverified |
| `submit` | Submit a link or text post by fetching HN's current hidden submission form token and posting directly (write, `--confirm` required; login required) | unverified |
| `updates` | Return recently changed item IDs and public profiles | unverified |
| `user` | Fetch a public Hacker News user profile and recent submission IDs | unverified |
| `user_activity` | Resolve a user's recent public submissions into compact stories, comments, jobs, or polls | unverified |
| `vote` | Vote or unvote by fetching the current item page, extracting HN's per-action auth URL, and replaying it direct (write, `--confirm` required; login required) | shape-verified |

</details>

<details>
<summary><b>Hugging Face</b> (huggingface.co) — 53 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `agent_harnesses_list` | List agent harnesses recognized by the Hugging Face Hub | verified |
| `collection_get` | Read a curated collection and all of its visible model, dataset, Space, and paper items | verified |
| `collections_list` | Discover and page through curated Hub collections by owner, text, or contained item | verified |
| `daily_papers_list` | Page through papers selected by the Hugging Face daily-papers community | verified |
| `dataset_croissant_get` | Return the machine-readable Croissant metadata record for a dataset | verified |
| `dataset_facets` | List the current dataset task, language, license, size, format, and other filter facets | verified |
| `dataset_get` | Return detailed metadata, files, tags, usage, and card information for one public dataset | verified |
| `dataset_info_get` | Return Dataset Viewer features, row counts, and configuration metadata | verified |
| `dataset_leaderboard_get` | Return leaderboard information published for a dataset when available | verified |
| `dataset_parquet_list` | List the generated Parquet files backing Dataset Viewer access | verified |
| `dataset_preview_rows` | Preview the first rows and column features of a dataset split | verified |
| `dataset_rows_filter` | Filter a dataset split with Dataset Viewer's SQL-like where expression | verified |
| `dataset_rows_get` | Read a bounded page of rows from a dataset configuration and split | verified |
| `dataset_rows_search` | Full-text search a dataset split and return matching bounded rows | verified |
| `dataset_size_get` | Return downloadable, generated, and total byte and row counts for a dataset | verified |
| `dataset_splits_list` | List every Dataset Viewer configuration and split available for a dataset | verified |
| `dataset_statistics_get` | Return computed statistics for the columns of a dataset split | verified |
| `dataset_viewer_status` | Check whether Dataset Viewer services are available for a public dataset | verified |
| `datasets_list` | Discover and page through public datasets with author, tag, query, and sort filters | verified |
| `discussion_get` | Read one repository discussion or pull request with its event history and comments | verified |
| `discussions_list` | Page through public discussions and pull requests attached to a repository | verified |
| `docs_list` | List the documentation products and versions indexed by Hugging Face search | verified |
| `docs_search` | Search current Hugging Face documentation, optionally within one product | verified |
| `hub_search` | Search models, datasets, Spaces, users, organizations, and other Hub resources in one request | verified |
| `hub_trending` | Return the resources currently trending across the Hugging Face Hub | verified |
| `kernel_get` | Return the public metadata for one Hugging Face Kernel | verified |
| `kernels_list` | Discover and page through public Hugging Face Kernels | verified |
| `model_facets` | List the current model task, library, language, license, and other filter facets | verified |
| `model_get` | Return detailed metadata, files, tags, usage, and card information for one public model | verified |
| `model_security_get` | Return the Hub security scan summary and reported file issues for a model | verified |
| `models_list` | Discover and page through public models with author, tag, query, and sort filters | verified |
| `organization_get` | Return a public Hugging Face organization profile and visible metadata | verified |
| `paper_get` | Return paper metadata, authors, discussion, votes, and linked Hub resources | verified |
| `paper_read` | Read Hugging Face's bounded Markdown rendering of a paper without opening a browser or PDF viewer | verified |
| `papers_search` | Search the Hugging Face paper index by title, abstract, author, or topic | verified |
| `profile_connections_list` | Page through a public profile's followers or followed accounts | verified |
| `repo_commits_list` | Page through repository commits at a branch, tag, or commit revision | verified |
| `repo_compare` | Compare two repository revisions and return their commit and file differences | verified |
| `repo_file_metadata` | Inspect a repository file's resolved URL, size, content type, and entity tag without downloading it | verified |
| `repo_folder_size` | Calculate the stored size of a repository folder at a chosen revision | verified |
| `repo_paths_get` | Resolve metadata for several exact repository paths in one bounded request | verified |
| `repo_refs_list` | List branches, tags, and conversion references for a repository | verified |
| `repo_text_file_read` | Read a bounded UTF-8 repository file and return its content and SHA-256 digest | verified |
| `repo_tree_list` | Browse a model, dataset, or Space repository tree at an exact revision without cloning it | verified |
| `space_api_describe` | Inspect the callable Gradio API contract published by a running public Space without invoking it | verified |
| `space_get` | Return detailed metadata, SDK configuration, host, and status information for one public Space | verified |
| `space_options_get` | List the current Space hardware choices or starter templates | verified |
| `space_runtime_get` | Return the current build stage, hardware, sleep state, and runtime details for a Space | verified |
| `spaces_list` | Discover and page through public Spaces with author, tag, query, and sort filters | verified |
| `spaces_semantic_search` | Find public Spaces by describing the task or behavior they should perform | verified |
| `tasks_list` | Return Hugging Face task definitions and their associated models and datasets | verified |
| `user_get` | Return a public Hugging Face user profile and its visible resources and activity | verified |
| `user_likes_list` | Page through the public repositories liked by a Hugging Face user | verified |

</details>

<details>
<summary><b>IBBI</b> (ibbi.gov.in) — 2 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `orders` | List IBBI's latest disciplinary and adjudication orders with document links | verified |
| `whats_new` | List the newest circulars, orders, and notices from IBBI's What's New page with document links | verified |

</details>

<details>
<summary><b>India Post</b> (postalpincode.in) — 2 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `pincode` | List all post offices serving a 6-digit Indian PIN code | verified |
| `post_office` | Find Indian post offices by branch name | verified |

</details>

<details>
<summary><b>Nominatim (OpenStreetMap)</b> (openstreetmap.org) — 2 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `reverse` | Reverse-geocode coordinates to the nearest OpenStreetMap address (Nominatim) | verified |
| `search` | Search OpenStreetMap for places and addresses by free-text query (Nominatim) | verified |

</details>

<details>
<summary><b>npm</b> (npmjs.com) — 17 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `audit_versions` | Check explicit public npm package versions against npm's bulk security advisory service | verified |
| `download_tarball` | Download one public npm package tarball and return a private local file receipt with its SHA-256 digest | verified |
| `get_download_count` | Return npm's aggregate download count for a package over a named period or explicit date range | verified |
| `get_download_history` | Return a bounded daily npm download series for up to 366 days with local cursor pagination | verified |
| `get_package` | Return a bounded package overview with the latest release, tags, maintainers, links, and README preview | verified |
| `get_provenance` | Return bounded npm publish and SLSA provenance attestations with transparency-log identifiers | verified |
| `get_readme` | Read the package README directly from the registry with an explicit character bound and truncation signal | verified |
| `get_registry_status` | Return npm's current overall status, component states, and active public incidents | verified |
| `get_version` | Resolve an exact version or distribution tag and return its normalized manifest, dependencies, and integrity d | verified |
| `list_dependencies` | List one package version's runtime, development, peer, optional, and bundled dependencies in normalized pages | verified |
| `list_dependents` | List packages that npm currently shows as depending on a public package, using npm's browser-compatible public | verified |
| `list_maintainer_packages` | List public packages associated with an npm maintainer account using the registry's maintained search index | verified |
| `list_package_files` | Inspect a published npm tarball and list its files, sizes, modes, and entry types without extracting it | verified |
| `list_versions` | List a package's releases in publication order with dates, deprecations, manifests, and distribution integrity | verified |
| `read_package_file` | Read one bounded regular file directly from a public npm tarball as UTF-8 text or base64 | verified |
| `read_site_resource` | Read a bounded public npmjs | verified |
| `search_packages` | Search the public npm registry with npm's text, keyword, maintainer, and scope query syntax | verified |

</details>

<details>
<summary><b>NSE</b> (nseindia.com) — 3 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `announcements` | List the latest corporate announcements filed with NSE by listed companies, with subjects, timestamps, and att | verified |
| `corporate_actions` | List an NSE-listed company's corporate actions (dividends, splits, bonuses) with ex-dates and record dates | verified |
| `shareholding` | List an NSE-listed company's shareholding-pattern disclosures (promoter and public holdings by quarter) | verified |

</details>

<details>
<summary><b>Open Brewery DB</b> (openbrewerydb.org) — 5 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `breweries` | Filtered directory listing of breweries (by state, city, type, and more) | verified |
| `brewery` | Fetch one brewery's full record by id | verified |
| `meta` | Total count of breweries in the directory | verified |
| `random` | Return one random brewery | verified |
| `search` | Free-text search over brewery names and cities | verified |

</details>

<details>
<summary><b>Open Library</b> (openlibrary.org) — 3 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `author_get` | Read one Open Library author record by author id, including name and birth date | verified |
| `search` | Search Open Library's public book catalog and return bounded matches with work keys, titles, and edition data | verified |
| `work_get` | Read one Open Library work record by work id, including title, description, and first publish date | verified |

</details>

<details>
<summary><b>Open-Meteo</b> (open-meteo.com) — 4 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `air_quality` | Current air quality (PM2 | verified |
| `archive` | Historical daily weather (max/min temperature, precipitation) for a coordinate and date range | verified |
| `forecast` | Current conditions and a daily forecast for any coordinates from Open-Meteo's free weather API | verified |
| `geocode` | Resolve a place name to coordinates using Open-Meteo's geocoding API | verified |

</details>

<details>
<summary><b>PyPI</b> (pypi.org) — 18 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `download_distribution` | Download one public distribution file and return a private local file receipt with its SHA-256 digest | verified |
| `get_core_metadata` | Read the PEP 658 core metadata (the packaged METADATA file) for one distribution without downloading the whole | verified |
| `get_description` | Read a project's long description (README) as bounded text with its declared content type | verified |
| `get_ownership` | List a project's owners and maintainers and its owning organization when present | verified |
| `get_project` | Fetch a public PyPI project's normalized metadata: latest version, summary, license, classifiers, project URLs | verified |
| `get_project_release_feed` | List a single project's recent releases from its public RSS feed | verified |
| `get_provenance` | Return the PEP 740 provenance attestation bundles for one distribution file when the uploader published them | verified |
| `get_release` | Fetch metadata and distribution files for one exact PyPI release version | verified |
| `get_stats` | Return PyPI's public storage statistics: total index size and the largest projects by stored bytes | verified |
| `get_vulnerabilities` | Return the OSV security advisories PyPI records for a project or exact release | verified |
| `list_all_projects` | List public PyPI project names from the PEP 691 simple index, optionally filtered by a normalized name prefix | verified |
| `list_dependencies` | Parse a release's PEP 508 requirements into normalized name, specifier, extra, and marker fields | verified |
| `list_files` | List the distribution files (wheels and sdists) for one exact release with digests, sizes, and yank status | verified |
| `list_recent_packages` | List the newest projects published to PyPI from its public RSS feed | verified |
| `list_recent_updates` | List the most recent release updates across PyPI from its public RSS feed | verified |
| `list_releases` | List a project's release versions newest first, with upload time, file count, and yank status | verified |
| `list_versions` | List all version strings for a project from the PEP 691 simple index, newest first | verified |
| `read_site_resource` | Read a bounded public pypi | verified |

</details>

<details>
<summary><b>RBI</b> (rbi.org.in) — 3 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `press_releases` | List today's RBI press releases from the press-release page with document links | verified |
| `press_releases_by_year` | List RBI press releases for a given year from the archive, with titles and links | verified |
| `press_releases_feed` | List the Reserve Bank of India's newest press releases from its official RSS feed | verified |

</details>

<details>
<summary><b>SEBI</b> (sebi.gov.in) — 1 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `rss_updates` | List SEBI's latest press releases, circulars, and orders from its official RSS feed | verified |

</details>

<details>
<summary><b>SEC EDGAR</b> (sec.gov) — 3 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `company_concept` | Fetch every reported value of one US-GAAP concept for a company, such as Revenues, from SEC XBRL data | verified |
| `company_profile` | Fetch a company's SEC registration profile (name, tickers, exchanges, industry) by CIK number | verified |
| `full_text_search` | Search the full text of SEC EDGAR filings and return matching documents with companies, forms, and dates | verified |

</details>

<details>
<summary><b>Spotify</b> (open.spotify.com) — 38 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `account` | Return the current Spotify account profile (login required) | shape-verified |
| `add_playlist_items` | Add up to 100 track or episode URIs to a playlist (write, `--confirm` required; login required) | shape-verified |
| `add_to_queue` | Add a track or episode URI to the playback queue (write, `--confirm` required; login required) | shape-verified |
| `api_request` | Bounded authenticated escape hatch for any Spotify Web API path (write, `--confirm` required; login required) | unverified |
| `auth_status` | Validate the retained normal Web Player login or optional official OAuth, and report local desktop playback av | unverified |
| `configure` | Optional: save a Spotify developer Client ID for the official PKCE transport (write, `--confirm` required) | unverified |
| `create_playlist` | Create and live-verify a public, private, collaborative, or described playlist through the retained Web Player (write, `--confirm` required; login required) | shape-verified |
| `currently_playing` | Get the current track or episode and progress (login required) | unverified |
| `delete_playlist` | Delete an owned playlist from Your Library through Spotify's retained Web Player rootlist protocol (write, `--confirm` required; login required) | shape-verified |
| `desktop_status` | Inspect the signed-in Spotify Desktop player on macOS without a developer Client ID | unverified |
| `devices` | List available Spotify Connect devices (login required) | shape-verified |
| `disconnect` | Delete the local Spotify OAuth tokens for this AgentWeb profile (write, `--confirm` required) | unverified |
| `library` | List saved tracks, albums, shows, episodes, audiobooks, followed artists, or current-user playlists (login required) | shape-verified |
| `library_contains` | Check whether Spotify URIs are in the current user's library (login required) | unverified |
| `next` | Skip to the next item (write, `--confirm` required; login required) | shape-verified |
| `pause` | Pause playback on the active or selected device (write, `--confirm` required; login required) | shape-verified |
| `play` | Play a query, track URI, or full album/artist/playlist context (write, `--confirm` required; login required) | shape-verified |
| `playback_state` | Get the current playback item, progress, context, device, repeat, and shuffle state (login required) | unverified |
| `playlist` | Fetch compact playlist metadata and one bounded page of compact items (login required) | shape-verified |
| `playlists` | List the current user's playlists (login required) | shape-verified |
| `previous` | Skip to the previous item (write, `--confirm` required; login required) | unverified |
| `queue` | Get the current playback queue (login required) | unverified |
| `recently_played` | Return Spotify Web Player Recents activity after a normal login (login required) | shape-verified |
| `remove_library` | Remove or unfollow Spotify URIs through the retained Web Player library mutation, then verify live membership (write, `--confirm` required; login required) | unverified |
| `remove_playlist_items` | Remove up to 100 track or episode URIs from a playlist (write, `--confirm` required; login required) | shape-verified |
| `reorder_playlist_items` | Move a contiguous range of playlist items (write, `--confirm` required; login required) | shape-verified |
| `repeat` | Set repeat mode to track, context, or off (write, `--confirm` required; login required) | shape-verified |
| `replace_playlist_items` | Replace all playlist items with up to 100 URIs (write, `--confirm` required; login required) | shape-verified |
| `resource` | Fetch one track, album, artist, playlist, show, episode, audiobook, or chapter (login required) | unverified |
| `save_library` | Save or follow Spotify URIs through the retained Web Player library mutation, then verify live membership (write, `--confirm` required; login required) | unverified |
| `search` | Search tracks without setup through public indexing, or search all catalog types through one normal retained S | shape-verified |
| `seek` | Seek the active item to a millisecond position (write, `--confirm` required; login required) | unverified |
| `setup_status` | Report zero-setup desktop playback and optional Web API authorization readiness without exposing secrets | unverified |
| `shuffle` | Enable or disable shuffle (write, `--confirm` required; login required) | shape-verified |
| `top_items` | Return the current user's top tracks or artists (login required) | unverified |
| `transfer_playback` | Transfer playback to one Spotify Connect device and optionally play (write, `--confirm` required; login required) | unverified |
| `update_playlist` | Change and live-verify a playlist's name, visibility, collaboration, or description through the retained Web P (write, `--confirm` required; login required) | shape-verified |
| `volume` | Set playback volume from 0 through 100 (write, `--confirm` required; login required) | unverified |

</details>

<details>
<summary><b>Stack Overflow</b> (stackoverflow.com) — 22 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `account_status` | Verify the retained Stack Overflow session and return its profile URL without exposing cookies | unverified |
| `add_comment` | Add a comment to a question or answer with automatic fkey handling (write, `--confirm` required; login required) | unverified |
| `answer` | Post an answer with automatic fkey handling (write, `--confirm` required; login required) | unverified |
| `answers` | List bounded answers for a question | unverified |
| `api_get` | Bounded read-only escape hatch for public Stack Exchange API endpoints not yet represented by a typed command | unverified |
| `ask` | Ask a question through Stack Overflow's current authenticated form protocol (write, `--confirm` required; login required) | unverified |
| `comments` | List bounded comments on a question or answer | unverified |
| `question` | Fetch one question and its highest-voted answers as bounded plain text | unverified |
| `question_timeline` | List public timeline events for a question and explicitly identify vote aggregates whose direction and count a | unverified |
| `questions` | List Stack Overflow questions | unverified |
| `related` | Find questions related to an existing question | unverified |
| `search` | Search Stack Overflow questions with optional tags and accepted-answer filtering | unverified |
| `similar` | Find questions with a similar title and optional tags | unverified |
| `submit_form` | Submit any current same-site HTML form, including edit, save, flag, and moderation forms, by selector (write, `--confirm` required; login required) | unverified |
| `tag_info` | Fetch public metadata for one tag | unverified |
| `tags` | List popular Stack Overflow tags, optionally filtered by name | unverified |
| `unanswered` | List unanswered questions with optional tag filters | unverified |
| `user` | Fetch one public Stack Overflow profile | unverified |
| `user_answers` | List a user's public answers with bounded bodies | unverified |
| `user_questions` | List questions created by a public user | unverified |
| `users` | Search or list public Stack Overflow users | unverified |
| `vote` | Upvote, downvote, accept, unaccept, or undo a vote with automatic fkey handling (write, `--confirm` required; login required) | unverified |

</details>

<details>
<summary><b>Wikipedia</b> (wikipedia.org) — 19 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `account_status` | Verify the retained Wikipedia session and report the authenticated user, groups, rights, and block state | unverified |
| `api_query` | Bounded read-only escape hatch for MediaWiki query, parse, opensearch, and help actions | unverified |
| `api_request` | Call a fixed-host MediaWiki Action API operation (write, `--confirm` required) | unverified |
| `backlinks` | List main-namespace pages that link to an article | unverified |
| `categories` | List the visible categories assigned to an article | unverified |
| `category_members` | List pages and subcategories inside a Wikipedia category | unverified |
| `edit` | Create or replace a Wikipedia page through the MediaWiki Action API (write, `--confirm` required; login required) | unverified |
| `images` | List image and media file pages used by an article | unverified |
| `languages` | List available language editions for an article | unverified |
| `links` | List main-namespace pages linked from an article | unverified |
| `nearby` | Find geotagged Wikipedia pages near coordinates | unverified |
| `page` | Read a Wikipedia page as bounded plain text with canonical metadata | shape-verified |
| `pageviews` | Return daily public pageviews and a total for a date range | unverified |
| `random` | Return random article titles and links from a Wikipedia language edition | shape-verified |
| `revisions` | List recent page revisions with editors, comments, sizes, and timestamps | unverified |
| `search` | Search Wikipedia article titles and text with compact snippets | shape-verified |
| `sections` | List an article's section hierarchy and anchors | unverified |
| `upload` | Upload a local file through MediaWiki's multipart Action API (write, `--confirm` required; login required) | unverified |
| `user_contributions` | List a public Wikipedia user's recent contributions | unverified |

</details>

<details>
<summary><b>World Bank</b> (worldbank.org) — 2 operations</summary>

| Operation | Description | Status |
| --- | --- | --- |
| `countries` | Directory of World Bank country and region codes with capital and coordinates | verified |
| `indicator` | Time series of a World Bank development indicator for one country (or 'all') | verified |

</details>

This repository is the open core: the command-line tool, runtime, login/session
system, adapter format, signed updater, tests, and the bundled adapters. The
automatic system used to map and repair the catalog is **not** in this repository.

> **Note on terms of service.** Some sites (notably Amazon and LinkedIn) restrict
> automated access in their terms of service. You are responsible for using these
> adapters in a way that complies with each site's terms and applicable law.

## How it works

```text
website mapped once -> versioned AgentWeb adapter -> reusable commands for every agent
```

Each adapter describes the website's actions, inputs, output, login needs, and
known gaps. AgentWeb keeps the common behavior in one runtime: domains, sessions,
request limits, confirmation for writes, structured errors, and safe updates.

### Capture-once drift verification

Any operation can earn a keyless, credential-free drift check without a developer
key. Capture one real successful run as a **response oracle** — the redacted
response *shape* plus optional named assertions, never any captured values — then
replay it later to confirm the site has not changed underneath the adapter:

```bash
agentweb capture-oracle npmjs.com get_version \
  --input '{"package":"react"}' --assert '$.data.version' --out react.oracle.json
agentweb verify-capture react.oracle.json --strict   # capture_verified | drift
```

A single capture becomes a permanent regression oracle: when the site changes its
response, `verify-capture` reports `drift` (and exits non-zero under `--strict`
for CI) instead of silently returning degraded data. For a **mutating** operation
the oracle records the read-back that confirms the effect, not the mutation, so
replaying it never re-changes account state — `capture-oracle` refuses a mutating
op unless you pass `--confirm`.

#### Browser-assisted read-back (`--via-browser`)

A few sites actively refuse browserless replay: their anti-bot layer bounces a
plain HTTP request into a redirect/challenge loop even with a valid session, so
they can never earn a browserless `capture_verified`. For these, run the read
*inside* the site's already-authenticated Chrome via CDP:

```bash
agentweb capture-oracle SITE OP --via-browser --assert '$.data.x' --out o.json
agentweb verify-capture o.json --strict   # or --via-browser
```

The adapter builds and parses the request exactly as usual; only the transport
changes — the request is issued with `fetch(url, {credentials:"include"})` from a
page on the site's own origin, so first-party cookies and anti-bot context apply.
The response flows back through the normal envelope, so the oracle still stores
**only** the response shape and assertion types — never response values, cookies,
or the browser profile. Such an oracle records `"execution": "browser_assisted"`,
`verify-capture` re-runs it in the browser automatically, and a passing replay is
reported as `browser_capture_verified` to keep it distinct from browserless
`capture_verified`. This path is explicit-only (ordinary typed operations never
launch a browser), requires a prior `agentweb connect SITE`, and is **read-only**.

#### Continuous oracle drift (`verify-oracles`)

One capture is a receipt; a *directory* of captures replayed on a schedule is a
standing guarantee. `verify-oracles` discovers every `*.oracle.json` under a
directory and replays each, so a site that quietly changes its response shape is
caught automatically instead of at the next manual check:

```bash
agentweb verify-oracles --dir oracles --strict   # exits non-zero on any drift
```

- **Keyless, browserless, non-mutating** oracles are replayed against the live
  site and reported `capture_verified` or `drift`.
- **Mutating** oracles (which record a read-back) and **browser-assisted**
  oracles are account-tied, so they are `skipped` by default; pass
  `--via-browser` to also replay the browser ones inside their authenticated
  Chrome.
- A transient network/site error is reported as `inconclusive` and does **not**
  fail the run; only genuine drift or an unreadable oracle fails `--strict`.
- `--offline` validates each oracle's structure without making any request.

The committed [`oracles/`](oracles/) directory holds keyless public-read oracles
(npm, arXiv, Wikipedia) that the scheduled **Oracle drift** workflow replays
weekly. A passing oracle proves the captured response shape and assertions still
match; it does not prove every mutation or site action works.

### Why Spotify uses the desktop app

This is a Spotify-specific path built into its AgentWeb adapter. For simple
playback on macOS, AgentWeb resolves a song to a Spotify URI, sends an Apple Event
to the installed Spotify app, and reads the player state back to verify what
happened. Account features such as playlists, the library, queue, and remote
devices use a saved Spotify Web Player session and Spotify Connect after one
normal login.

AgentWeb provides the common machinery for adapters to use direct website
requests, official APIs, saved sessions, or local app bridges. It does not
automatically control the desktop app for every website. A similar fast path can
be added when another service exposes a usable local protocol, URL scheme, or
operating-system automation interface, but it must be implemented and verified
for that service.

The browser can still appear when the website itself requires a person to log in,
solve a CAPTCHA, use a passkey, enter a one-time code, accept legal terms, or
confirm payment. It is not the normal path for already-mapped actions.

## Login only when needed

Try public commands without logging in. If an account action needs authorization,
AgentWeb returns `authentication_required`. Then the user runs:

```bash
agentweb connect example.com
```

By default this opens **already signed in**, reusing the sessions in your everyday
Chrome, so you rarely have to log in from scratch. It copies only the signed-in
session files (cookies, login data, `Local State`) from your default Chrome
profile into that site's login profile — never your history or extensions — and
only the target site's cookies are kept, so profiles still never share cookies
across sites. It seeds once per site, never overwrites a captured session, and
falls back to a blank window when no default Chrome profile is found. Pass
`--isolated` (or `AGENTWEB_USE_DEFAULT_BROWSER=0`) to force a blank window.

Sessions stay on that user's device under `~/.agentweb`. They are not bundled into
an adapter or shared with other users. Read [security and trust](docs/SECURITY.md)
before installing an authenticated third-party adapter.

## Build an adapter

The open core includes the public pieces needed to build and distribute an adapter:

```bash
agentweb adapter-new example --base-url https://example.com --root ./registry
agentweb registry-build ./registry
agentweb audit example --root ./registry
```

Start with [building an adapter](docs/BUILDING_ADAPTERS.md). The documentation is
explicit about login, write confirmation, evidence, incomplete coverage, and what
must never be committed.

## Documentation

| If you want to… | Read… |
| --- | --- |
| Use AgentWeb from an agent | [Agent guide](docs/AGENT_GUIDE.md) |
| Connect Claude Code or Codex | [Agent setup](docs/AGENT_HOSTS.md) |
| Understand the code | [Architecture](docs/ARCHITECTURE.md) |
| Build a website adapter | [Adapter guide](docs/BUILDING_ADAPTERS.md) |
| Understand sessions and safety | [Security](docs/SECURITY.md) |
| Understand usage analytics | [Analytics](docs/ANALYTICS.md) |
| Contribute code | [Contributing](CONTRIBUTING.md) |

Coding agents working on this repository should read [AGENTS.md](AGENTS.md).
`llms.txt` provides a compact machine-readable map of the documentation.

## Current status

AgentWeb is an early public preview. Websites can change endpoints, revoke
sessions, add verification, or rate-limit requests. AgentWeb reports an adapter's
known gaps and verification state instead of claiming that every action will work
forever.

The public core is licensed under [Apache-2.0](LICENSE). Official adapter bundles,
hosted services, and the private mapping system may use separate terms.
