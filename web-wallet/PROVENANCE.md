# Source and modifications

All workspace packages are private local forks, retained under their original
names so Coco imports resolve to the modified cashu-ts. Their original runtime
source is vendored, rather than depending on unpublished modifications in a
different checkout. Upstream tests, build output, and unrelated monorepo packages
are omitted. Each package retains its upstream license.

| Package                   | Upstream source                          | Pinned revision                                       | Base version |
| ------------------------- | ---------------------------------------- | ----------------------------------------------------- | ------------ |
| `packages/cashu-ts`       | https://github.com/cashubtc/cashu-ts     | `a313e71bdf39e94a4b795eea670833e2a2aef09a` (`v3.3.0`) | 3.3.0        |
| `packages/coco`           | https://github.com/a1denvalu3/coco       | `2a0ee454ded4a13f0fd9369b26b6026efcadf312`            | 1.1.2-rc.42  |
| `packages/coco-indexeddb` | same Coco checkout, `packages/indexeddb` | same revision                                         | 1.1.2-rc.42  |

cashu-ts 3.3.0 is the version this Coco revision targets. The upstream standard
Cashu exports remain available. New `tmp-cash` subpath and root exports add
`PSWallet`, PS wire schemas, token codecs, BOLT11 validation, and a byte-compatible
TypeScript port of this repository's v3 PS credential/proof implementation.
Cryptographic arithmetic, pairing, and hash-to-curve use pinned Noble libraries;
the interoperability tests exercise the Python verifier directly.

The Coco fork adds `TmpCashManager` and typed repository contracts. It follows
Coco's prepare/reserve/execute/finalize lifecycle and uses its original
`EventBus`. The IndexedDB fork adds `TmpCashIndexedDbRepository` on Coco's original
`IdbDb` transaction wrapper. PS proofs have their own schema; standard Cashu
proof rows are not reused. Root and `tmp-cash` exports expose these additions.

Workspace manifests expose TypeScript source to Vite and type checking, pin
runtime dependencies, and link local forks. The ordinary upstream APIs are
retained as source but are not exercised by this wallet's tests. No upstream
checkout is changed and no package is published.

The PS port derives from `tmp_cash/crypto/ps.py`, `tmp_cash/keyset.py`, `tmp_cash/models.py`,
and `tmp_cash/client.py` in this repository. See the repository's root
`PROVENANCE.md` for its original cashu-nft attribution. The wallet app and these
changes are covered by the root MIT license.
