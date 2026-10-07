# Source provenance

The PS scheme was extracted from the local checkout of
[`callebtc/cashu-nft`](https://github.com/callebtc/cashu-nft), commit
`bb70771695330389dca4ee86d75497f736394bf4`, under its MIT license.
The original license is retained in `LICENSE.md`.

Retained code:

- `cashu/core/crypto/bls.py`: pyblst BLS12-381 point and scalar wrappers.
- `cashu/core/crypto/ps.py`: key derivation, credentials, pairing checks,
  owner-secret proofs, rerandomized public/private spending presentations,
  multi-witness linear proofs, mint-controlled swap bases, blind reissuance,
  and client unblinding.

Removed code includes image hashing and duplicate tags, historical blinded
image issuance protocols, image metadata, all marketplace/portfolio code,
frontend applications, and unrelated Nutshell mint/wallet services.

The new mint/client and Lightning integration are independent Python modules.
Initial issuance accepts a public payment hash, whole invoice amount, and
`C = s·G1`, retaining fresh mint-chosen signing randomness. The signed `h`
attribute hashes the complete payment hash and amount before reduction into Fr.
The mint computes this public attribute from the funded quote. Bearer tokens
carry both the owner secret and the original HTLC preimage. Burning reveals
the attribute, verifies the supplied backing preimage and full amount, and pays
an invoice with an independent payment hash before settling the backing hold.
Private transfers retain the private presentation and equality witnesses,
with equality of `h` preserving the whole HTLC claim. The current commitment
is `B = h·Y_h1 + t·G1`; the mint returns `U = k·G1` and
`V_raw = k·(x·G1 + B + y_s·C_new)` together, and the client subtracts `t·U`.
This adapts PS signing of message commitments from
[section 6.1 of The Landscape of Pointcheval-Sanders Signatures](https://eprint.iacr.org/2020/450.pdf)
and removes the preliminary signing-base request. The earlier equations are
retained only to recover saved requests and their original receipts.
Changes to the extracted primitives isolate
the keyset domain as `ps-htlc-claim-v2`, validate keyset identity, admit `h = 0`
as a valid Fr value, reject explicit zero rerandomization, and expose a separate pairing-only
authenticity check. A common mint key, transaction receipts, Lightning
redemption bindings, and client recovery storage are added by this project.

The original checkout and its local files were retained.
