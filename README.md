# Cashu Layer3

An experimental Python mint and wallet for **denominated tokens backed by held
Lightning payments**. Credentials use Pointcheval–Sanders (PS) signatures on
BLS12-381. A token can split into smaller denominations and transfer privately
while its original HTLC remains held. A Lightning melt can spend tokens from
several HTLCs, including user-funded routing fees and returned change.

**Tokens expire with their backing.** Settlement, cancellation, or timeout ends
the claim. Holders must redeem before the published redemption cutoff. There
are no user balances or continuing redemption rights after settlement.

The [offline protocol guide](docs/layer3.html) illustrates the v3 flow.

## The lifecycle

1. The payer chooses a 32-byte preimage `P` and supplies `H = SHA256(P)` and an
   amount `A`. The mint creates a HODL invoice. Once fully accepted, it signs
   denomination tokens whose total equals `A`. Issuance does not reveal `P`.
2. The signed backing attribute is `h = HashToFr(domain || A || H)`. Every
   fragment retains this **original HTLC amount**, independently of its own
   denomination. One keyset contains independent PS keys for `1, 2, 4, 8, ...`
   sats, through the supported monetary limit.
3. A swap has many inputs and many outputs, all sharing one hidden `h`.
   Public denomination values must sum equally. A single common-witness proof
   establishes equality across every input presentation and output commitment.
   Swaps can split, combine, or refresh fragments of **one HTLC**; they cannot
   move value between different backing hashes.
4. A melt can combine denominations from multiple HTLCs to pay one ordinary
   BOLT11 invoice. It reveals each backing preimage, reserves every input
   nullifier, and pays from the mint's outgoing liquidity. The receiver invoice
   has its own independent payment hash.
5. The mint charges invoice value plus the actual routing fee, rounded up from
   millisatoshis to whole sats. It returns unused fee reserve and any additional
   input value as denomination tokens tied to their original HTLCs.
6. A partial melt leaves the original hold pending. The mint settles a known
   preimage when the entire HTLC value has been consumed **including fees**, or
   at the published cutoff near the node's hold-cancellation deadline. Any
   outstanding fragments then expire. With no revealed preimage, the mint
   cannot settle; the hold eventually times out and tokens lose their backing.

For example, a 1,000-sat hold can become Alice's 990 sats and Bob's 10 sats.
Bob can pay a 9-sat invoice with a 1-sat fee reserve. If routing is free, he
receives 1 sat of change and the hold remains pending. At scheduled settlement,
Alice's 990 sats and Bob's 1 sat expire if still outstanding.

This intentionally forfeits unredeemed token value at settlement. Collecting
an HTLC collects its full amount, even if only a fraction was melted. The mint
does not turn the remainder into user accounts or an ongoing ecash liability.
A first revealed preimage also gives the mint the technical ability to settle
early; delaying settlement is mint policy. This remains a trusted,
experimental protocol, not a cryptographic guarantee against a dishonest mint.

## Run the demo

Requires Python 3.10+ and Poetry:

```bash
poetry install
make demo
```

The persistent fake backend runs in a temporary directory. The demo mints
1,000 sats, sends exactly 10 through a private split, pays 9 sats, returns unused
fee reserve, and settles at the cutoff so remaining tokens expire. No Lightning
node or server is needed.

## Run a mint

```bash
poetry run mint --backend fake --data data/mint-v3
```

For LND, copy `.env.example` to `.env`, configure the node's REST endpoint,
TLS certificate and macaroon, and choose a **new v3 mint directory**:

```bash
poetry run mint --backend lnd --data data/lnd-v3 \
  --lnd-endpoint https://127.0.0.1:8081 \
  --lnd-cert /path/to/tls.cert \
  --lnd-macaroon /path/to/admin.macaroon \
  --fee-limit-sat 10 \
  --lnd-hold-expiry-delta 18 \
  --settle-margin-blocks 6
```

TLS verification is enabled. The mint loads `.env` in the current directory;
environment variables override the file and CLI arguments override both. Run
one server process per database and retain its seed across restarts. The mint
needs outgoing liquidity for payments while its incoming holds remain pending.

`--lnd-hold-expiry-delta` must match the node's `invoices.holdexpirydelta`.
LND can cancel accepted holds before their CLTV expiry. The redemption cutoff
is **earliest accepted HTLC expiry − node cancellation delta − settlement
margin**; multipart payments use the earliest accepted part. The default
settlement margin is 6 blocks before cancellation. The fake backend uses a
120-second margin before its wall-clock expiry (`--settle-margin-seconds`).
Choose margins for your node's operating conditions. The background worker
runs every 5 seconds; it cannot settle through a node outage or while stopped.

## Wallet commands

The default mint is `http://127.0.0.1:3338` and the default wallet database is
`data/wallet-v3.sqlite3`. Select another mint or wallet using global flags:

```bash
poetry run cashu --mint http://127.0.0.1:3338 --wallet data/alice-v3.sqlite3 \
  mint 1000 <payment_hash> --preimage <32-byte-lowercase-hex>
```

The command prints the HODL invoice and waits for funding. Resume an interrupted
mint with `mint --quote <quote_id>`; its saved commitments and secrets are reused.
The preimage is stored locally and carried with token proofs.

```bash
# Split automatically, retaining local change. Send chooses one backing HTLC.
poetry run cashu --wallet data/alice-v3.sqlite3 send 10
# If several HTLCs can fund the send, choose --payment-hash <hash>.

# Receiver downloads the full backing list, then refreshes ownership privately.
poetry run cashu --wallet data/bob-v3.sqlite3 receive <cashuPS-token>

# Can combine different HTLCs. Covers invoice + reserve and returns fee change.
poetry run cashu --wallet data/bob-v3.sqlite3 pay <bolt11> --fee-reserve 1

poetry run cashu htlcs
poetry run cashu list
poetry run cashu balance --refresh --verbose
poetry run cashu pending
poetry run cashu retry <operation_id>
poetry run cashu abandon <operation_id>
```

`melt` aliases `pay`. Repeating `--token-id` selects specific inputs for a melt,
or recovers the exact bundle of `SENT` proofs after lost send output. `decode`
prints a bearer bundle without spending it. Fake development helpers are
`dev-accept <hash>` and `dev-invoice <amount>`.

A send requires one HTLC with enough value. A received bundle containing
multiple HTLCs is processed as separate swaps, each with its own saved recovery
operation. Payments may combine them. An unavailable or incomplete public
backing snapshot fails the acceptance check. No hash-specific request reveals
which HTLC the receiver is inspecting.

Local `LIVE` state and an unrefreshed balance are not proof of current backing.
`balance --refresh` marks unavailable claims `EXPIRED`; it never revives spent
proofs. The public full list is a mint-reported snapshot, not independent
collateral verification, and backing can change after the check.

## Melt fees, allocation and recovery

`POST /v1/melt/quote/bolt11` returns the exact payout invoice, amount, fee reserve
and invoice expiry. The default reserve equals `--fee-limit-sat`; the wallet may
request a smaller reserve, including zero. A reserve above the server limit is
rejected. LND receives that authorized limit via
[SendPaymentV2](https://lightning.engineering/api-docs/api/lnd/router/send-payment-v2/).
The returned and tracked `fee_msat` is retained through retries; the charged fee
is `ceil(fee_msat / 1000)`.

A combined melt charges HTLC groups in order of their first appearance in the
inputs. It consumes the first group's selected value before charging the next.
Change stays with its original group. Before sending, the wallet prepares one
owner commitment for each possible binary change denomination per group. Once
the actual fee is known, the mint signs only the required subset. All input
metadata, input order, the exact melt quote and change commitments are bound
into every spending proof.

The mint reserves input nullifiers, each group's selected face value and the
payout hash in one SQLite transaction. It retains the revealed backing
preimages before payment. A confirmed payment, change signatures and consumed
value are stored atomically **before** full-consumption settlement. A response
lost after payout is recovered from Lightning's tracked payment without paying
again. Exact retries return the original signatures and receipt.

A definite payout failure releases all inputs. An uncertain or pending payout
keeps them reserved. Wallet abandonment requires **every** input to be unspent.
Failure cannot make a disclosed preimage unknown again: the mint retains it for
scheduled settlement. A paid melt receipt says `PAID`, regardless of whether
its backing HTLCs remain held or have subsequently closed.

Already authorized payments may still resolve after the cutoff. Recovery
continues to reconcile those payments, but creates no claim against a closed
HTLC. Such change is reported as `expired_change`, rather than a user balance
or newly spendable token. New melts against closed claims are rejected.

## Cryptography

For input `i` under denomination key `a_i`, the randomized presentation contains
`κ_i = h·Y_h2[a_i] + o_i·G2`. For output `j` under denomination key `b_j`, the
blinded commitment is `B_j = h·Y_h1[b_j] + t_j·G1`.

One Fiat–Shamir linear proof uses witnesses `(h, o_1, ..., o_n, t_1, ..., t_m)`
to prove **all** these equations simultaneously. The shared witness establishes
all-to-all equality of `h`, without an `n × m` collection of pairwise proofs.
Each input independently proves credential authenticity and ownership of its
nullifier. The full ordered input/output destination transcript is bound into
the ownership and equality proofs.

The mint separately checks `sum(a_i) = sum(b_j)`, rejects duplicate inputs and
output commitments, consumes all input nullifiers atomically, and returns
fresh blind signatures. The client subtracts `t_j·U_j` to unblind each output.
Independent denomination keys prevent relabeling small credentials as larger
ones. Denominations and transaction totals are visible to the mint; backing
hashes, backing amounts, `h` and preimages remain hidden during swaps.

A keyset ID commits to the complete ordered denomination/public-parameter map.
Credentials use that common ID; verification selects the key by denomination.
`h` commits to the full 32-byte hash and original amount before scalar reduction.

## API and limits

FastAPI provides schemas at `/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/info`, `GET /v1/keys` | Capabilities and denomination public keys |
| `GET /v1/htlcs/pending` | Fresh full backing list and redemption cutoffs |
| `POST /v1/mint/quote/bolt11` | Supplied-hash HODL invoice |
| `GET /v1/mint/quote/bolt11/{quote}` | Funding/issuance status |
| `POST /v1/mint/bolt11` | Issue a denomination batch for the full funded amount |
| `POST /v1/swap` | Private, value-conserving, same-HTLC many-to-many swap |
| `POST /v1/melt/quote/bolt11` | Payout quote and routing fee reserve |
| `POST /v1/melt/bolt11` | Combined melt and original-HTLC fee change |
| `POST /v1/checkstate` | `UNSPENT`, `PENDING`, or `SPENT` nullifier states |

Batches allow up to 256 inputs or outputs; melt change commitments also have a
256-slot limit. For very large mixed-HTLC batches, use smaller payments or select
fewer inputs. Bearer bundles use experimental `cashuPS` JSON/base64 encoding,
with token version 3, and are incompatible with ordinary Cashu wallets.

Version-2 tokens, saved operations and databases are deliberately rejected.
There is no automatic conversion of old whole-claim balances into v3. Preserve
old databases and complete their claims using the previous version; use fresh
mint and wallet directories for this protocol.

## Verification

```bash
make test
make check
make demo
make regtest
```

The live runner uses an existing funded, synced cashu-regtest network and does
not provision nodes, fund channels or mine blocks. Its full-value melt requests
a zero fee reserve, requiring a fee-free route (normally a direct channel) to
avoid needing an extra token beyond the held amount. CLI options can override
node containers, REST endpoint and node hold-cancellation delta.

Source attribution is retained in [PROVENANCE.md](PROVENANCE.md). MIT licensed.
