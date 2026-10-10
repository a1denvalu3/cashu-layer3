# Source provenance

The PS primitives were extracted from
[`callebtc/cashu-nft`](https://github.com/callebtc/cashu-nft), commit
`bb70771695330389dca4ee86d75497f736394bf4`, under its MIT license.
The original license is retained in `LICENSE.md`.

`cashu/crypto/bls.py` retains the pyblst BLS12-381 point/scalar wrappers.
`cashu/crypto/ps.py` retains key derivation, credential and pairing checks,
owner-secret/nullifier proofs, randomized public/private presentations,
multi-witness linear proofs, blind signing and client unblinding. Historical
swap helper equations remain available as primitives, but the v3 API does not
accept or recover v2 requests or databases.

Image hashing, duplicate-image tags, NFT metadata, historical image issuance,
marketplace/portfolio code, frontend applications and unrelated Nutshell
mint/wallet services were removed. The Lightning backend, mint/client state
machines and wallet persistence are independent modules in this project.

## Denomination protocol v3

Each binary denomination has an independent, domain-separated PS signing key.
The common keyset ID commits to the complete ordered amount/parameter map.
Credentials and presentations retain their original byte encodings; signature
verification selects the denomination's parameters. The primitive public-key
wrapper now allows the enclosing keyset ID to be supplied.

The signed backing attribute hashes the complete payment hash and original
HTLC amount before reduction into Fr. A batch swap uses the existing
`LinearProof` primitive with one common `h` witness and separate blinders for
every input presentation and output commitment. It proves equality across all
original-claim attributes. Public input/output denomination sums must also
match, and duplicate inputs are rejected before atomic nullifier consumption.

Blind outputs use `B = h·Y_h1 + t·G1`. Fresh mint randomness produces
`U = k·G1`, `V_raw = k·(x·G1 + B + y_s·C_new)` in one response; the client
subtracts `t·U`. This adapts PS signing of message commitments from
[section 6.1 of The Landscape of Pointcheval–Sanders Signatures](https://eprint.iacr.org/2020/450.pdf).

The denomination keyset, batch proof statements, combined melts, routing-fee
reserves/change, terminal HTLC lifecycle and multi-input recovery storage were
added in this project. Application transcripts and the claim scalar use v3
domains. V2 mint/wallet databases and tokens are rejected instead of being
reinterpreted. Nullifier bases and low-level proof encodings are retained.
Tokens expire with their backing; delayed settlement creates no post-closure
user balances or continuing redemption rights.
