# Cashu

A Python mint and wallet for a Layer3 built on held Lightning payments.
Each token represents one whole HTLC claim, signed with
Pointcheval–Sanders (PS) signatures on BLS12-381. Holders can transfer that
claim through private swaps while the original Lightning payment stays pending.
The final holder burns the token to pay a Lightning invoice and settle its
backing HTLC.

## How Layer3 works

The client chooses a 32-byte preimage `P` and computes its payment hash
`H = SHA256(P)`. The mint creates a HODL invoice for that hash and the requested
amount. Once the full payment is accepted and held by the Lightning node, the
mint signs exactly one token for the entire invoice amount.

```text
Lightning: payer → mint's HODL invoice; the HTLC remains held
Cashu:     Alice → Bob → Carol; each receiver privately swaps the token
Burn:      mint pays the holder's invoice and settles the original HTLC
```

Transfers use the mint's swap API and do not settle the backing payment or
create additional Lightning payments. Every swap has one input and one output
for the same HTLC claim. There are no denominations, splits, combinations, or
change: a 12,345-sat token must be transferred or burned as a whole.

A bearer token carries two distinct secrets:

| Secret | Purpose | What happens on a swap |
|---|---|---|
| Owner secret `s` | Authorizes spending and derives the credential's nullifier | Replaced with a fresh secret |
| HTLC preimage `P` | Lets the mint settle the original held payment at burn | Preserved and passed to the new holder |

The **backing HTLC hash and payout invoice hash are independent**. Burning
pays an ordinary invoice for the token's full amount. The wallet supplies `P`
with its spending proof; the mint pays the invoice, verifies the payout's own
preimage, then settles the original HODL invoice using `P`.

| Phase | What the mint learns |
|---|---|
| Minting | Backing payment hash, full amount, and owner commitment; the mint computes the signed claim attribute `h` |
| Private swap | Randomized credential, nullifier, new owner commitment, and blinded claim commitments/proofs; `h`, backing hash, amount, and HTLC preimage stay hidden |
| Burning | Public claim attribute, backing HTLC preimage and amount, plus the payout invoice |

The transferring wallets know the token's amount, backing hash, and secrets.
The swap's privacy applies to the information sent to the mint.

## Install and run the demo

Use Python 3.10 or later and Poetry. Run commands from this checkout:

```bash
poetry install
poetry run python examples/demo.py
```

The demo uses a persistent fake Lightning backend in a temporary directory.
It mints a 1000-sat claim, transfers it from Alice to Bob to Carol, pays an
invoice with an unrelated hash, and settles the original hold. It requires
no Lightning nodes.

## Run a mint

For a local fake backend:

```bash
poetry run mint --backend fake --data data/mint
```

For LND, `mint` loads `.env` from the current directory. On first setup, copy
[.env.example](.env.example) to `.env` and provide the node's TLS certificate
and macaroon at the configured paths. The example selects cashu-regtest's
`lnd-3`, with its REST endpoint at `https://localhost:8081`:

```bash
poetry run mint
```

Environment variables override `.env`; command-line options override both.
The startup message identifies the selected backend. The default wallet mint
URL is `http://127.0.0.1:3338`; use `cashu --mint <url> ...` to select another.
The default wallet database is `data/wallet.sqlite3`.

An explicit LND configuration is also supported:

```bash
poetry run mint \
  --backend lnd \
  --data data/lnd-mint \
  --lnd-endpoint https://127.0.0.1:8080 \
  --lnd-macaroon /path/to/admin.macaroon \
  --lnd-cert /path/to/tls.cert \
  --fee-limit-sat 10
```

TLS verification is enabled. The backend uses LND's
[AddHoldInvoice](https://lightning.engineering/api-docs/api/lnd/invoices/add-hold-invoice/),
[SendPaymentV2](https://lightning.engineering/api-docs/api/lnd/router/send-payment-v2/),
and [SettleInvoice](https://lightning.engineering/api-docs/api/lnd/invoices/settle-invoice/)
APIs. The mint needs outgoing liquidity for payouts and pays routing fees
from its own balance, within the configured fee limit.

Run one server process per mint database. Keep the mint data directory and
seed across restarts; the signing key and redemption recovery records live
there. Restart the server after updating the code. `.env` and `data/` are
excluded from Git.

## Mint, transfer, and burn on regtest

This example uses an already running, synced, funded
[cashu-regtest](https://github.com/callebtc/cashu-regtest) network. Set it up
separately. The commands use Docker, `jq`, OpenSSL, and `xxd`, and do not depend
on another local checkout.

The nodes are `lnd-1` as payer, `lnd-3` as the mint's backend, and `lnd-2` as
the final payout receiver.

### 1. Start the mint in terminal 1

For first-time configuration:

```bash
mkdir -p data/lnd-manual
cp .env.example .env
docker cp cashu-lnd-3-1:/root/.lnd/tls.cert data/lnd-manual/tls.cert
docker cp cashu-lnd-3-1:/root/.lnd/data/chain/bitcoin/regtest/admin.macaroon \
  data/lnd-manual/admin.macaroon

poetry run mint
```

If you already configured `.env`, reuse it. The server should report
`Mint backend: lnd`.

### 2. Request funding in terminal 2

Generate the backing HTLC preimage and hash. SHA256 hashes the decoded
32 bytes of the preimage:

```bash
PREIMAGE=$(openssl rand -hex 32)
PAYMENT_HASH=$(printf '%s' "$PREIMAGE" | xxd -r -p |
  openssl dgst -sha256 -r | cut -d ' ' -f1)

poetry run cashu --wallet data/alice.sqlite3 mint 12345 "$PAYMENT_HASH" \
  --preimage "$PREIMAGE"
```

The mint creates its HODL invoice; no payout invoice is needed yet. The command
prints the quote JSON, including `request` (the BOLT11 invoice) and `quote`
(its ID), and waits for funding. `--preimage` saves the secret locally with
the issuance operation and resulting token. It is not sent to the mint at
this stage.

### 3. Fund the HODL invoice in terminal 3

Paste the quote's `request` into:

```bash
docker exec cashu-lnd-1-1 lncli \
  --network=regtest --rpcserver=lnd-1:10009 \
  payinvoice --force "<paste the BOLT11 request from terminal 2>"
```

`--force` skips the confirmation prompt. The Lightning payment remains
pending because the mint has not received its preimage. Once LND reports full
HTLC acceptance, terminal 2 automatically mints the token, saves it in Alice's
wallet, and exits. The payer command stays pending until burn.

### 4. Transfer the token in terminal 2

```bash
poetry run cashu --wallet data/alice.sqlite3 balance

TOKEN=$(poetry run cashu --wallet data/alice.sqlite3 send 12345)
poetry run cashu --wallet data/bob.sqlite3 receive "$TOKEN"
poetry run cashu --wallet data/bob.sqlite3 balance
```

`send` marks Alice's token `SENT` locally and prints a bearer token string
containing the owner secret and original HTLC preimage. `receive` reads the
mint URL from that string, verifies the claim and preimage, and privately
swaps into a fresh owner secret. The original credential's nullifier becomes
spent at the mint. Bob retains the same amount, backing hash, and HTLC preimage.

### 5. Burn Bob's token to a normal receiver invoice

Create an invoice for the full amount on `lnd-2`. LND generates the payout's
own preimage and payment hash:

```bash
RECEIVER_INVOICE=$(docker exec cashu-lnd-2-1 lncli \
  --network=regtest --rpcserver=lnd-2:10009 \
  addinvoice --amt 12345 | jq -r '.payment_request')

poetry run cashu --wallet data/bob.sqlite3 pay "$RECEIVER_INVOICE"
poetry run cashu --wallet data/bob.sqlite3 balance
```

`pay` selects an available token by the invoice's full amount. Its payout hash
does not have to match the token's backing hash. Bob's wallet supplies the
original HTLC preimage automatically from the token.

A successful burn returns `state: "SETTLED"`, pays `lnd-2`, settles the original
HODL on `lnd-3`, and reduces Bob's available balance to zero. The payer command
in terminal 3 finishes successfully. The receipt's `payment_hash` and
`preimage` describe the backing HTLC; `payout_hash` and `payout_preimage`
describe the receiver payment.

Verify both invoices independently:

```bash
PAYOUT_HASH=$(docker exec cashu-lnd-2-1 lncli \
  --network=regtest --rpcserver=lnd-2:10009 \
  decodepayreq "$RECEIVER_INVOICE" | jq -r '.payment_hash')

docker exec cashu-lnd-2-1 lncli \
  --network=regtest --rpcserver=lnd-2:10009 lookupinvoice "$PAYOUT_HASH"

docker exec cashu-lnd-3-1 lncli \
  --network=regtest --rpcserver=lnd-3:10009 lookupinvoice "$PAYMENT_HASH"
```

Both invoice states should be `SETTLED`.

For a fake-backend CLI test, use `mint --backend fake --data data/mint`,
the same mint/send/receive commands, and
`cashu dev-accept <backing_payment_hash>` in another terminal to simulate
funding. Create the payout with `cashu dev-invoice 12345` and use its returned
`request` in `cashu pay`. These helpers use `data/mint/fake.sqlite3` by default;
pass `--backend-db` when using a different fake data directory.

## Wallet commands and recovery

Place global `--wallet` and `--mint` options before the command. In the
following examples, prepend `poetry run` when running from this checkout:

| Command | Behavior |
|---|---|
| `cashu mint <amount> <payment_hash> --preimage <preimage>` | Create a HODL quote, wait for full acceptance, and save one token |
| `cashu mint --quote <quote_id>` | Resume a saved issuance request and its funding wait |
| `cashu quote <amount> <payment_hash>` | Return a funding quote without waiting or minting |
| `cashu send <amount>` | Export one whole token string and mark it `SENT` locally |
| `cashu receive <token>` | Verify and privately swap a received token |
| `cashu pay <invoice>` | Burn one token with the invoice's full amount |
| `cashu balance` | Show the available `LIVE` balance |
| `cashu list` | Show token IDs, amounts, backing hashes, and local states |
| `cashu decode <token>` | Decode the bearer token JSON without spending it |
| `cashu pending` | List saved operations for recovery |
| `cashu pending --sent` | List locally sent tokens |
| `cashu retry <operation_id>` | Retry the exact saved operation |
| `cashu abandon <operation_id>` | Release a failed swap/burn locally after the mint confirms it is unspent |

If multiple available tokens have the requested amount, `send` and `pay`
require `--token-id <id>`. They do not split or combine tokens. Mint selection
can also be restricted with the global `--mint` option.

Ctrl+C stops a mint's funding wait and preserves its saved operation, including
the owner secret and locally supplied HTLC preimage. Resume with `mint --quote`
using the same wallet and mint. The HODL invoice remains open or held.

Wallets save operations before sending a spending request. After an interrupted
connection or lost response, use `pending` and `retry` to recover the original
result. Exact retries return their original receipts. A payout with an
uncertain result leaves the token `PENDING`; the mint tracks it and resumes
settlement after a restart. A definite payout failure releases the token at
the mint, after which `abandon` can release the wallet's saved operation.

If a send's output was lost, `send <amount> --token-id <id>` returns the same
saved bearer string. Receiving that string back into the sender's wallet
reclaims the claim through a swap if its nullifier is still unspent.

### Existing tokens without an HTLC preimage

Attach the original backing preimage when sending or paying:

```bash
poetry run cashu send 12345 --preimage "$PREIMAGE"
poetry run cashu pay "$RECEIVER_INVOICE" --preimage "$PREIMAGE"
```

These are alternative actions for the existing token. The supplied preimage
must hash to its backing payment hash. Once attached to a sent token, it
travels with the bearer string and survives subsequent private swaps. You can
also add `--preimage` when resuming a mint quote that was saved without it.

## Token format

Tokens use the prefix `cashuPS`, followed by unpadded base64url-encoded JSON:

| Field | Contents |
|---|---|
| `version` | `2` |
| `mint` | Mint URL |
| `amount` | Full HTLC amount in sats |
| `payment_hash` | Original backing HTLC hash, as 32-byte lowercase hex |
| `credential` | PS signature, claim attribute, keyset ID, and owner secret |
| `preimage` | Original backing HTLC preimage, as 32-byte lowercase hex |

Older tokens may omit `preimage`; attach it as described above before sending
or burning. Token strings and wallet databases contain spending secrets.
This is an experimental PS encoding; ordinary Cashu wallets cannot decode
or spend it.

The receiver rebuilds `h` from the declared amount and backing hash, verifies
the PS credential, and checks that the supplied preimage hashes to that
backing hash. Relabeling a 1-sat claim as 1,000,000 sats invalidates the
credential under the same mint key.

## Cryptography

All scalar arithmetic is modulo the BLS12-381 scalar field order `q`.
`G1` and `G2` are the respective group generators. The mint's secret key is
`(x, y_h, y_s)`, with public points:

```text
X2   = x · G2
Y_h2 = y_h · G2
Y_s2 = y_s · G2
Y_h1 = y_h · G1
```

### Public issuance

The mint computes the signed claim attribute from its funded quote:

```text
H = SHA256(P)
h = int(SHA256(domain || amount_be64 || H), big-endian) mod q

C = s · G1
u = k · G1
v = k · [(x + y_h · h) · G1 + y_s · C]
```

`domain` is the exact ASCII string `Cashu_PS_HTLC_Claim_v2`. `amount_be64` is
the full amount in sats, encoded as an unsigned 8-byte big-endian integer,
and `H` is the full 32-byte hash. One mint key signs every supported amount.

The client proves knowledge of the nonzero owner secret `s` behind `C`.
The mint chooses fresh, secret, nonzero `k` and returns `(u, v)` together after
confirming that the entire HODL invoice is accepted and funded. The client
cannot choose a different amount or `h` in the issuance request.

Fresh secret `k` blocks the fixed-base attribute forgery. With `u = G1`, an
attacker could replace `h` with `h★` using
`v★ = v + (h★ − h) · Y_h1`. With `u = k · G1`, this change requires
`y_h · u = k · Y_h1`, a computational Diffie–Hellman computation in `G1`.
The client's later credential randomization provides privacy.

### Private swaps and nullifiers

The holder chooses fresh randomness `ρ`, `o`, `t` and a new owner secret
`s_new`, with `C_new = s_new · G1`:

```text
u′   = ρ · u
v″   = ρ · v + o · u′
κ_h  = h · Y_h2 + o · G2
U_s  = s · u′
N    = s · G_NULL
B    = h · Y_h1 + t · G1
```

`G_NULL` is a public hash-to-group base. The mint verifies the randomized
credential through the pairing equation:

```text
e(v″, G2) = e(u′, X2 + κ_h) · e(U_s, Y_s2)
```

The owner/nullifier proof has witness `s` and public points
`(G_NULL, u′, N, U_s)`. It proves both relations using the same secret:

```text
U_s = s · u′
N   = s · G_NULL
```

The claim equality proof has witnesses `(h, o, t)` and public points
`(κ_h, B, Y_h2, G2, Y_h1, G1)`. It proves:

```text
κ_h = h · Y_h2 + o · G2
B   = h · Y_h1 + t · G1
```

Both spending proofs bind `C_new`. The mint also verifies knowledge of the
new owner secret behind that commitment. Together, these checks preserve the
same signed `h` and tie the input credential to its nullifier without
revealing the claim or owner secret.

The mint atomically consumes `N`, chooses fresh `k`, and returns:

```text
U     = k · G1
V_raw = k · [x · G1 + B + y_s · C_new]
V     = V_raw − t · U
```

The client unblinds `V_raw` to obtain a credential for `(h, s_new)`. This uses
one `POST /v1/swap`, with `(U, V_raw)` returned in the same response. It adapts
PS signing of message commitments
([section 6.1](https://eprint.iacr.org/2020/450.pdf)).

Rerandomizing an input credential does not change `N`, so it cannot bypass
the spent-nullifier check. A successful swap replaces `s` and therefore gives
the output credential a new nullifier. The HTLC preimage remains bearer data;
it is not a swap-proof witness and is not sent in the swap request.

### Public authentication and burning

A credential's signature can be checked through a pairing equation, using
`U_s = s · u`:

```text
e(v, G2) = e(u, X2 + h · Y_h2) · e(U_s, Y_s2)
```

`verify_signature` requires neither `s` nor the HTLC preimage as inputs.
The holder's `verify_credential` check uses its local credential and creates
no ZK proof. Spending additionally requires the owner/nullifier proof.
There is no separate show endpoint.

At burn, the holder sends the original preimage, a public-`h` spending
presentation, and the payout invoice. The mint recomputes the backing hash
from the preimage and checks `h` against that hash and full amount. The
spending proof binds the payout invoice and amount. The payout invoice's
own hash is used to track its payment; the supplied backing preimage settles
the original hold.

## API

FastAPI serves request and response schemas at `/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/info` | Protocol capabilities |
| `GET /v1/keys` | Common PS public parameters |
| `POST /v1/mint/quote/bolt11` | Create a supplied-hash HODL invoice |
| `GET /v1/mint/quote/bolt11/{quote}` | Funding and issuance status |
| `POST /v1/mint/bolt11` | Sign the owner commitment after full HTLC acceptance |
| `POST /v1/swap` | Privately preserve the whole claim and replace its owner secret |
| `POST /v1/checkstate` | Nullifier states: `UNSPENT`, `PENDING`, or `SPENT` |
| `POST /v1/burn` | Pay a whole-amount invoice and settle the backing hold |

A mint quote accepts `amount`, `unit: "sat"`, `payment_hash` (32-byte lowercase
hex), and optional `expiry`. It creates the HODL invoice through the node and
returns its BOLT11 request. Neither quoting nor issuance receives the HTLC
preimage.

Current swap requests use `version: 2`, with a private presentation, new owner
commitment/proof, blinded commitment `b`, and claim equality proof.

A burn request contains:

| Field | Meaning |
|---|---|
| `amount` | Whole token amount in sats; must equal the payout invoice amount |
| `presentation` | Randomized spending presentation with public `h` and nullifier proof |
| `request` | Payout BOLT11 invoice, whose hash can be unrelated to the backing HTLC |
| `preimage` | Original backing HTLC preimage |

The mint reserves the nullifier, backing claim, and payout hash. It stores
the confirmed payout receipt before settling the backing hold, allowing
recovery without paying twice. A settled response includes `amount`,
`payment_hash`, `preimage`, `payout_hash`, and `payout_preimage`.

## Compatibility and scope

Current version-2 tokens, keys, and mint databases remain usable. Tokens
missing their preimage can attach it through `send --preimage` or
`pay --preimage`. Saved requests from the earlier two-round swap and same-hash
burn flows remain recoverable through `cashu retry`.

Earlier payment-hash-only tokens and populated databases belong to a different
protocol; they require a new database for this whole-claim scheme.

Tokens are conditional claims on held payments. Cancellation or HTLC timeout
can remove their backing even if a credential's signature still verifies.
The private swap does not reveal the backing hash, so the mint does not query
that individual hold during a swap. The mint remains trusted for custody and
settlement. This prototype and its cryptography are experimental.

Splitting, combining, change, cross-mint redemption, and interoperability with
ordinary Cashu token formats are outside this protocol.

## Verification

```bash
make test
make check
make demo
```

Run the live integration test against your existing cashu-regtest network:

```bash
make regtest
```

It verifies one 12,345-sat token, two private swaps while the original payer
remains pending, payment of an unrelated receiver invoice, settlement of the
backing hold, and idempotent retries.

The defaults use containers `cashu-lnd-1-1` as payer, `cashu-lnd-3-1` as mint,
and `cashu-lnd-2-1` as receiver, with the mint's REST API at
`https://localhost:8081`. The test copies credentials into a temporary
directory. Missing or stopped containers, unsynced nodes, insufficient channel
liquidity, or an unavailable or mismatched REST endpoint cause an error.

The runner does not start or stop nodes, mine blocks, fund wallets, or open
channels. Override node names and the REST endpoint with `--payer-container`,
`--mint-container`, `--receiver-container`, and `--lnd-endpoint`:

```bash
poetry run python tests/regtest/run.py --help
```

The BLS12-381 PS primitives were extracted from `cashu-nft`. Source attribution
and implementation changes are recorded in [PROVENANCE.md](PROVENANCE.md);
the original MIT license is retained in [LICENSE.md](LICENSE.md).
