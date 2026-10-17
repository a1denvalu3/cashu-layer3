# tmp.cash web wallet

A responsive browser wallet for the PS/BLS12-381 mint in this repository. Modified
cashu-ts handles credentials and wire requests; modified Coco manages proofs,
funding preimages, saved operations, activity, and IndexedDB persistence. The app
does all wallet cryptography in the browser. It works with the mint's fake, LND,
and Spark backends through the same HTTP API.

## Run

Start your configured mint from the repository root, for example:

```sh
poetry run tmp.cash-mint --backend lnd --data data/lnd-v3
```

Use the [root README's Spark configuration](../README.md#spark-with-the-breez-python-sdk) for a
Spark mint. The web wallet needs the mint URL; it does not need Spark operator
configuration, a mint mnemonic, or Lightning node credentials.

In another terminal:

```sh
cd web-wallet
npm ci
npm run dev
```

Open **http://127.0.0.1:5173** for the explainer, then choose **Open wallet**.
The wallet is also available directly at **http://127.0.0.1:5173/wallet/**.
The cryptography explainer is at **http://127.0.0.1:5173/how-it-works/**,
linked from the landing page navigation, diagram, and footer.
The default mint is
**http://127.0.0.1:3338**. Change it in Settings, or set `VITE_DEFAULT_MINT` before
starting/building. Node 22.4+ is required. Fonts and scripts are bundled locally.

The root mint API enables CORS for its public GET/POST endpoints. No private
wallet data is served by the frontend. For a remote deployment, serve `dist/`
over HTTPS and use an HTTPS mint; browsers block mixed-content requests.

## Use

- **Landing page:** explains frozen Lightning deposits and private ecash
  transfers, with an animated Alice–mint–Carol diagram showing what the mint sees
  and what stays hidden. Its alternative deadline scenario expires Alice’s full
  balance before a transfer, or both holders’ balances after a transfer, and
  reverts the deposit to Alice after the Lightning hold times out.
  Expiry is explained above the diagram. Further questions cover redemption and
  mint trust. It opens without connecting to a mint or initializing wallet
  storage. **Open wallet** takes you to the
  wallet; **How tmp.cash works** in the wallet footer returns to the explainer.
- **Cryptography:** a separate article explains the v3 backing attribute,
  denomination signatures, owner proofs and nullifiers, private swaps, Lightning
  melts, expiry, and mint trust. A split/combine example illustrates value and
  backing conservation. Equations and expandable proof details follow the
  implementation, including the difference between initial issuance and blind
  reissuance. It opens without a mint connection or wallet storage.
- **Add sats:** generates and durably saves a backing preimage, requests a HODL
  invoice with its hash, and displays the invoice/QR. Pay from a Lightning wallet.
  Once the payment is held (`ACCEPTED`), the wallet mints denomination proofs.
  The payer's wallet can continue to show a pending payment until settlement.
  The wallet shows only the newest invoice still awaiting payment. **Deposit
  invoice history** opens all invoices for the selected mint, newest first,
  including funded and closed invoices. Select an entry to reopen it. Older
  waiting invoices continue to be checked for payment in the background.
- **Send:** picks one backing with enough value, privately splits its proofs,
  retains change, and saves the outgoing `cashuPS` token in Activity. Its preimage
  travels with the token. Sent proofs are excluded from the balance.
- **Receive:** checks the signature, mint, backing, deadline, and nullifiers,
  then swaps into fresh owner secrets. If any backing has less than 20 minutes
  until its redemption deadline, the wallet asks for confirmation before saving
  or swapping the token. Block-based deadlines use an estimate of 10 minutes per
  remaining block, with a reminder that actual block times vary. Canceling leaves
  the token untouched. Mixed bundles are refreshed with a
  separate swap per backing. All backing proofs and prepared requests are saved
  together before the first swap, so every part remains recoverable if a response
  is lost or the tab closes. Tokens from another mint require explicitly
  connecting to that mint first. A backing without a preimage offers an
  **Add backing preimage** action.
- **Pay with Lightning:** paste an invoice, scan its QR code with your camera,
  or upload a QR image. Camera scanning requires HTTPS or localhost and camera
  permission. Plain invoices and `lightning:` QR payloads are supported, including
  uppercase invoices. Scanning fills the invoice field; choose **Review payment**
  to check the amount and fees before confirming. The wallet verifies the signed
  BOLT11 invoice, requests the mint's fee quote, and shows its amount and the
  mint's fee reserve. The wallet reserves
  enough ecash for that quote and combines eligible backings when necessary. Each melt
  reveals the complete preimage of every backing used. Change stays tied to its
  original backing; expired change is shown in the payment receipt.
  An explicit pending response triggers bounded automatic checks of the exact
  saved request. If completion takes longer, the payment stays in Pending
  operations with its inputs reserved and a Retry action; it is shown as pending.
- **Appearance:** use the header’s sun/moon button to switch modes, or choose
  Light, Night, or System in Settings. The preference is saved in this browser;
  System follows your device’s color scheme.
- **Settings:** exports/restores password-encrypted backups containing proofs,
  backing preimages, pending request secrets, and the outgoing token history.
  Restore requires an empty wallet in a new browser profile. A snapshot backup
  cannot recover operations created after it was downloaded.

The wallet verifies credentials and refreshes backing and nullifier status.
Expired, settled, canceled, sent, reserved, or remotely pending proofs are
excluded from the available balance. An unavailable mint produces an unverified
balance. Nothing becomes a claim against a mint balance after HTLC settlement.
Backups do not extend deadlines.

Expired backings remain visible below active backings and in Activity, with the
amount held separately from tokens already shared. Their observation time is
saved on the proofs and included in encrypted backups; it records when this
wallet detected expiry, not the exact settlement or deadline time. These local
records remain visible offline. Existing expired records acquire an observation
time on the next successful status check. If verification later recovers a live
claim, its expired entry disappears along with the incorrect expired status.

Background status checks use the same wallet lock as minting and spending, so
a snapshot taken before issuance cannot expire the newly stored outputs. Reloading
also rechecks proofs previously marked expired: a live backing, valid credential,
and an unspent nullifier are required before restoring spendable value. Outgoing
tokens stay excluded from the balance, and closed backing stays unusable.
Slow background refreshes share one pending request instead of filling the
operation queue. Funding polling continues with other deposits if one quote
cannot be checked, and saved issuance requests replay even after backing closes.
That replay recovers records; it does not make genuinely expired tokens spendable.

Operations save secrets and the **exact request before any spending POST**.
Retries after a timeout or reload replay that request. Paying the same invoice
again while an operation is pending reuses its saved request. A timeout or
uncertain failure keeps its input reservations. A definite payout failure or
duplicate-request rejection releases them only after the mint confirms every
input is unspent. Pending operations appear with a
Retry button. Browser Web Locks coordinate tabs; IndexedDB transactions also
prevent duplicate reservations. Clearing browser data without a current backup
loses the wallet. Serve this origin only with code you trust: live wallet data
is stored in IndexedDB, and backup encryption does not encrypt that database.

These are experimental `cashuPS` tokens, incompatible with standard Cashu token
formats. Backing status depends on the mint/backend's reported snapshot; see the
root README for the Breez SDK's cached-status limitation.

The wallet checks only its known backing hashes through `POST /v1/htlcs/check`,
including tokens being received. Requests contain at most 100 distinct hashes;
larger wallets use multiple batches. Checks disclose those hashes to the mint,
and a batch can associate several backings with one wallet. No preimages are
sent for a backing check. Update the frontend together with the mint: the old
unfiltered `/v1/htlcs/pending` endpoint is no longer available.

## Build and verify

```sh
npm run build
npm test
npx playwright install chromium
npm run test:e2e
npm run test:e2e:production
```

Build produces a static `dist/` with separate `index.html`,
`how-it-works/index.html`, and `wallet/index.html`
entries, each with a Content Security Policy. Serve the complete directory from
the same origin so the landing page and wallet share browser preferences and
existing IndexedDB funds. The `/wallet/` and `/how-it-works/` entries support direct visits and reloads
without requiring a server-side SPA fallback. `npm test` runs
cross-language integration tests against a disposable Python mint, including
private swaps, combined melts, fee change, deadlines, double-spend reservations,
lost quote/issuance/swap/melt responses, and authenticated backup encryption.
Playwright tests the actual desktop/mobile app, cross-origin mint requests,
two wallets, reload/retry, payout, expiry, and offline behavior. Browser tests
start their own disposable mint on port 3339 and frontend on port 5173. They use
fake sats and do not touch `~/cashu-regtest` or an existing mint database.

The modified libraries are source workspaces under `packages/`; no globally
installed packages or sibling checkouts are required. See
[PROVENANCE.md](PROVENANCE.md) for exact upstream revisions and changes.
