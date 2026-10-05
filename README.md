# AFSER private data service

This is the separate backend/MCP project for the AFSER map. The GitHub Pages
frontend contains application code only. The owner's computer retains the full
source payloads in a private SQLite database; authenticated clients receive
active anonymized records filtered to their selected chapter.

The database defaults to `../.private-data` relative to this checkout, the local
workspace location requested by the owner. The parent repository and this
project must both ignore it; keep every private file out of tracked Git paths.
This project has no example personal dataset and
does not manufacture live records when a source is unavailable.

## Run locally

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

```sh
cd /path/to/AFS/mcp
uv sync --locked
uv run afser-data sync
uv run afser-data serve
```

`sync` reads all configured Germany source pages into the local database and
prints counts only. `serve` binds to `127.0.0.1:8765` and syncs immediately and
every 30 minutes. `serve --no-poll` serves the last successful snapshot without
running source sync. Configure a different interval in private `bridge.json`
using `pollSeconds` (minimum 60). A session failure retains the last complete
snapshot; the status endpoint reports `source_session_expired` when re-login is
needed. No HTTP endpoint can initiate sync or alter source data.

After importing the first nationwide snapshot, `serve --skip-initial-sync`
starts scheduled updates after one polling interval, avoiding a duplicate
immediate import while retaining automatic updates.

To serve the generated frontend from the same local origin, use
`uv run afser-data serve --frontend-dir ../docs` and open
`http://127.0.0.1:8765/sending`. Static serving is restricted to frontend file
extensions and rejects hidden paths, traversal and symlinks. The data API still
requires a pairing token.

For a different data directory, provide `--data-dir /absolute/private/path`
before the subcommand, or set `AFSER_PRIVATE_DIR`. The directory has mode `0700`;
the database, keys, source config, token digests and invite files have mode `0600`.

## Pair the website

Create a local invite for the development site:

```sh
uv run afser-data invite --website http://localhost:5173 --label owner
open /path/to/AFS/.private-data/invite.html
```

The CLI writes a private HTML file and prints its path. It never prints the
token or invite URL. The HTML link puts the API endpoint and bearer token in a
URL fragment (`#api=…&token=…`), which is not sent to GitHub Pages. The frontend
should consume/remove that fragment and keep the token only in the browser's
session. API requests carry `Authorization: Bearer …`; credentials in query
parameters are rejected. The data responses have `Cache-Control: no-store`.

For remote volunteers, keep the bridge bound to loopback and expose it through
an HTTPS tunnel, such as [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).
Then create an invite with the real GitHub Pages base and tunnel URL:

```sh
uv run afser-data invite \
  --website https://YOUR-ACCOUNT.github.io/YOUR-MAP \
  --endpoint https://YOUR-HTTPS-TUNNEL \
  --label volunteer --days 30
```

The invite command updates the exact website origin and tunnel hostname in the
private bridge configuration. The server reloads those settings on requests.
Sharing an invite grants its recipient anonymous access across chapters,
including the explicit "all" view. Tokens expire after the requested number of
days (maximum 365). To revoke every invite:

```sh
uv run afser-data revoke-all
```

The owner's computer and tunnel need to remain running for remote map access.
Source links point back to AFSER, where AFSER's own login and signup controls apply.

## API contract

All data endpoints require a valid bearer token. `GET /api/status` without a
valid token returns only service health and `authenticationRequired: true`.
Browser origins must exactly match private `bridge.json`; wildcard origins are
rejected. Requests, connections and rates are bounded. Authentication uses
constant-time comparisons against locally stored SHA-256 token digests.

| Endpoint | Result |
| --- | --- |
| `GET /api/status` | Authenticated: `{ready,updatedAt,counts,privacy,sync}` |
| `GET /api/chapters` | `{chapters:[{id,name}]}` |
| `GET /api/places?q=Berlin` | `{places:[{id,city,chapterId,location:{lat,lon}}]}` |
| `GET /api/records?kind=sending&chapter=BER` | `{records,updatedAt,chapter,kind}` |
| `GET /api/records?kind=hostees&chapter=all` | All active hostees, explicitly requested |

Kinds are `sending`, `hopees`, `hostees` and `families`. A chapter must be an
explicit ID or `all`; there is no implicit default to all chapters. The filter
runs in SQLite before serialization. Every record is created by one shared
allowlist projection:

```text
id: opaque keyed identifier
kind, chapterId, status, urgent, deadline, country
sourceUrl: verified HTTPS afser.de record link
city: optional city name for sending, hostees and families
location: optional {lat,lon,radiusKm}
```

No source names, street addresses, household postcodes, phone numbers, email
addresses or raw payload fields are included. Household coordinates become a
stable randomly shifted public postal-locality point, with a displayed 1 km
uncertainty radius. The result reveals no distance to the home inside that cell.
Hopees have no city or home location; an explicit public destination-country
centroid may be included with `scope: country` and `radiusKm: 0`.

Verified open Sending tasks with no source chapter or postcode are retained as
`chapterId: unassigned` without a point. They appear only in the explicit All
view, keep their AFSER signup link, and do not add a fabricated chapter to the
chapter selector. Real chapter views exclude them.

Place search reads local public city/postal centroid data. Postal input is used
for finding a city; household postcodes are never returned. A city's chapter
assignment must come from configured source geography. When no assignment is
available, the frontend must ask the user to choose a chapter.

## MCP

The service uses the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
on its supported v1 maintenance line, pinned below 2 to preserve this transport
API. Launch it as a stdio MCP server:

```sh
uv run afser-data mcp
```

An MCP client configuration can use:

```json
{
  "mcpServers": {
    "afser": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/AFS/mcp", "afser-data", "mcp"]
    }
  }
}
```

Tools are `status`, `list_chapters`, `query_records(kind,chapter,limit=50)` and
`sync`. `query_records` returns at most 200 anonymized active records. `sync`
stores bulk source data locally and returns aggregate counts only. No MCP tool
or resource can return raw source payloads or exact home locations.

Allow a 600-second MCP tool timeout for `sync`, because complete nationwide
pagination can take several minutes. Tool annotations identify local queries
as read-only with no external access, and sync as a repeatable local write that
accesses the authenticated source. Sync does not modify AFSER records.

## Source adapter and atomic sync

Private `source.json` selects `{"adapter":"afser",…}`. The authenticated AFSER
adapter resides in `src/afser_data/afser.py`; its login and source-schema details
are documented there. Auth/session files belong in the private data directory.
Never put credentials in this repository, a GitHub Actions secret, a URL, or a
static frontend build.

For a new installation, create private `source.json` with your own login name
and an absolute path to a separate owner-only password file:

```json
{
  "adapter": "afser",
  "username": "YOUR AFSER LOGIN",
  "passwordFile": "/absolute/private/path/.afser-password"
}
```

The password file contains the password alone. The adapter reads it only into
the existing AFSER login form and persists authenticated cookies privately. It
restricts all acquisition requests to verified read-only AFSER routes; POST is
reserved for login. It never submits interview signup forms. City geometry
comes from downloaded public GeoNames postcode centroids and country geometry
from Natural Earth, joined locally without sending household addresses to a
geocoding provider. Original source HTML/API payloads remain private as well.

`AfserSource(config,private_dir).fetch()` returns a `SourceSnapshot` containing
chapter metadata, optional public city centroids, and a lazy iterable of
`RawRecord` values. A raw record can retain any source entity payload, even if
it has no active map projection. Records with a `Record` projection pass through
the shared privacy allowlist. Reading source pages and writing raw SQLite rows
does not print or send the Germany dataset to the model.

Sync uses a cross-process file lock and a single database transaction. A new
snapshot becomes active only after every page/record validates; expired
sessions, pagination cycles, duplicate identities and malformed classifications
roll back the transaction. The prior snapshot remains readable. The generic
`JsonReader` helper accepts read-only GET requests restricted to verified HTTPS
AFSER hosts, bounded response size and complete pagination. Logs contain
counts and constant error codes only, never exceptions carrying source content.

## Verify

```sh
uv run pytest -q
```

Tests cover projection allowlists, home-coordinate coarsening, country-only
Hopees, chapter/active filtering, full-sync rollback, private permissions,
bearer expiry/revocation, CORS/Host protection, rate limits, read-only source
routes, complete chapter partitions/board pagination, and a real MCP stdio
initialize/tool-call exchange.


## Explicit public GitHub export

The website owner explicitly authorized publishing anonymous locations and destination countries. `export-public --output ../docs/data` writes only approved active projection fields and public locality metadata. München is the default chapter; home circles have 1 km radius with stable keyed random centres based on public postal centroids. Raw payloads and credentials remain local. The exporter refuses extra private fields, old 10 km projections and export targets inside private storage.

`reproject-cached` rebuilds the local projection from existing complete private caches with all network access disabled. It preserves the source acquisition timestamp. The website repository's `scripts/update_public.py` handles scheduled source sync, export and audited publication. No raw-source endpoint is added to MCP or HTTP.

Source GET requests retry temporary network and server failures up to three times; authentication failures and login POSTs fail immediately. Duplicate source identities reject an incomplete import. Source metadata commits in the same SQLite transaction as its records, and sync status is written before releasing the import locks. A failed import retains the previous complete snapshot and its source timestamp.
