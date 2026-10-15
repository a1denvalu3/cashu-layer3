# cashu3 web wallet

A responsive browser wallet for the PS/BLS12-381 mint in this repository. Modified
cashu-ts handles credentials and wire requests; modified Coco manages proofs,
funding preimages, saved operations, activity, and IndexedDB persistence. The app
does all wallet cryptography in the browser. It works with the mint's fake, LND,
and Spark backends through the same HTTP API.

## Run

Start your configured mint from the repository root, for example:

```sh
poetry run mint --backend lnd --data data/lnd-v3
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
The default mint is
**http://127.0.0.1:3338**. Change it in Settings, or set `VITE_DEFAULT_MINT` before
starting/building. Node 22.4+ is required. Fonts and scripts are bundled locally.

The root mint API enables CORS for its public GET/POST endpoints. No private
wallet data is served by the frontend. For a remote deployment, serve `dist/`
over HTTPS and use an HTTPS mint; browsers block mixed-content requests.

## Use

- **Landing page:** explains funding, private transfers, redemption, expiry, and
  mint trust, with an interactive 1,000-sat example. It opens without connecting
  to a mint or initializing wallet storage. **Open wallet** takes you to the
  wallet; **How cashu³ works** in the wallet footer returns to the explainer.
- **Add sats:** generates and durably saves a backing preimage, requests a HODL
  invoice with its hash, and displays the invoice/QR. Pay from a Lightning wallet.
  Once the payment is held (`ACCEPTED`), the wallet mints denomination proofs.
  The payer's wallet can continue to show a pending payment until settlement.
- **Send:** picks one backing with enough value, privately splits its proofs,
  retains change, and saves the outgoing `cashuPS` token in Activity. Its preimage
  travels with the token. Sent proofs are excluded from the balance.
- **Receive:** checks the signature, mint, backing, deadline, and nullifiers,
  then swaps into fresh owner secrets. Mixed bundles are refreshed with a
  separate swap per backing. All backing proofs and prepared requests are saved
  together before the first swap, so every part remains recoverable if a response
  is lost or the tab closes. Tokens from another mint require explicitly
  connecting to that mint first. A backing without a preimage offers an
  **Add backing preimage** action.
- **Pay with Lightning:** verifies the signed BOLT11 invoice, requests the mint's
  fee quote, and shows its amount and the mint's fee reserve. The wallet reserves
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

## Build and verify

```sh
npm run build
npm test
npx playwright install chromium
npm run test:e2e
npm run test:e2e:production
```

Build produces a static `dist/` with separate `index.html` and `wallet/index.html`
entries, each with a Content Security Policy. Serve the complete directory from
the same origin so the landing page and wallet share browser preferences and
existing IndexedDB funds. The `/wallet/` entry supports direct visits and reloads
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
