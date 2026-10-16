import { useEffect, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Check,
  Fingerprint,
  LockKeyhole,
} from "lucide-react";
import {
  PublicPageFooter,
  PublicPageHeader,
  WalletLink,
} from "./PublicPageChrome";
import "./landing.css";
import "./cryptography.css";

const sections = [
  ["backing", "A deposit with a secret"],
  ["issuance", "What the mint signs"],
  ["ownership", "Ownership and receiving ecash"],
  ["swaps", "A private swap"],
  ["redemption", "Back to Lightning"],
  ["trust", "Expiry, privacy, and trust"],
] as const;

function Equation({ children }: { children: ReactNode }) {
  return (
    <pre className="crypto-equation">
      <code>{children}</code>
    </pre>
  );
}

function SectionTitle({ index }: { index: number }) {
  const [id, title] = sections[index];
  return (
    <h2 id={`${id}-title`}>
      <span className="mono">0{index + 1}</span> {title}
    </h2>
  );
}

function SwapExample() {
  const [combine, setCombine] = useState(false);
  const inputs = combine ? [32, 16, 16] : [64];
  const outputs = combine ? [64] : [32, 16, 16];

  return (
    <figure className="crypto-swap" aria-labelledby="swap-example-title">
      <figcaption className="crypto-swap-heading">
        <span id="swap-example-title">Same backing. Fresh credentials.</span>
        <div
          className="crypto-swap-controls"
          role="group"
          aria-label="Swap example"
        >
          <button aria-pressed={!combine} onClick={() => setCombine(false)}>
            Split
          </button>
          <button aria-pressed={combine} onClick={() => setCombine(true)}>
            Combine
          </button>
        </div>
      </figcaption>
      <div className="crypto-swap-backing">
        <LockKeyhole size={16} aria-hidden="true" />
        <span>One 64 sat Lightning hold</span>
        <span className="mono">SAME h</span>
      </div>
      <div className="crypto-swap-flow">
        <div
          className="crypto-swap-side"
          role="group"
          aria-label="Input credentials"
        >
          <span className="eyebrow">INPUTS · 64 SATS</span>
          <div className="crypto-notes">
            {inputs.map((amount, i) => (
              <div className="crypto-note" key={i}>
                <strong>
                  {amount} <span>sats</span>
                </strong>
                <span className="mono">h · s{i + 1}</span>
              </div>
            ))}
          </div>
          <span className="crypto-swap-caption">Old nullifiers are spent</span>
        </div>
        <ArrowRight
          className="crypto-swap-arrow"
          size={22}
          aria-hidden="true"
        />
        <div
          className="crypto-swap-side"
          role="group"
          aria-label="Output credentials"
        >
          <span className="eyebrow">OUTPUTS · 64 SATS</span>
          <div className="crypto-notes">
            {outputs.map((amount, i) => (
              <div className="crypto-note crypto-note-new" key={i}>
                <strong>
                  {amount} <span>sats</span>
                </strong>
                <span className="mono">h · s′{i + 1}</span>
              </div>
            ))}
          </div>
          <span className="crypto-swap-caption">Fresh owner secrets</span>
        </div>
      </div>
      <p
        className="crypto-swap-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <Check size={16} aria-hidden="true" />
        {combine
          ? "Three credentials become one. All 64 sats keep the same backing and deadline."
          : "One credential becomes three. All 64 sats keep the same backing and deadline."}
      </p>
      <p className="crypto-swap-footnote">
        Illustration only. The mint sees the denominations in this swap, but the
        shared backing attribute h is hidden by the proofs.
      </p>
    </figure>
  );
}

export function CryptographyPage() {
  useEffect(() => {
    // The article mounts after the browser's first attempt to follow a fragment.
    const hash = window.location.hash;
    if (!hash) return;
    let active = true;
    void document.fonts.ready.then(() => {
      if (active && window.location.hash === hash)
        document.getElementById(hash.slice(1))?.scrollIntoView();
    });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="landing cryptography-page">
      <a className="landing-skip" href="#main">
        Skip to content
      </a>
      <div className="landing-shell">
        <PublicPageHeader cryptography />
        <main id="main">
          <section className="crypto-hero" aria-labelledby="crypto-title">
            <span className="eyebrow hero-eyebrow">
              <span /> tmp.cash / PROTOCOL V3
            </span>
            <h1 id="crypto-title">
              The cryptography
              <br />
              behind <span>temporary cash.</span>
            </h1>
            <p className="crypto-lead">
              A held Lightning payment becomes ecash you can split and pass on.
              Signatures prove its value. Zero-knowledge proofs hide its backing
              during a swap. The backing’s deadline follows every fragment.
            </p>
            <div className="crypto-view-links" aria-label="Explanation views">
              <a href="/#how-it-works">
                The basics <ArrowUpRight size={14} aria-hidden="true" />
              </a>
              <a href="/how-it-works/?view=cryptography" aria-current="page">
                <Fingerprint size={15} aria-hidden="true" /> Cryptography
              </a>
            </div>
          </section>
          <div className="crypto-layout">
            <nav className="crypto-toc" aria-label="On this page">
              <span className="eyebrow">FOLLOW THE PROTOCOL</span>
              {sections.map(([id, title], index) => (
                <a href={`#${id}`} key={id}>
                  <span className="mono">0{index + 1}</span>
                  {title}
                </a>
              ))}
              <span className="crypto-toc-note">
                PS signatures · BLS12-381
                <br />
                Denominations · Held Lightning
              </span>
            </nav>
            <article
              className="crypto-article"
              aria-label="The tmp.cash cryptography"
            >
              <section id="backing" aria-labelledby="backing-title">
                <SectionTitle index={0} />
                <p>
                  Your wallet chooses a random 32-byte preimage <code>P</code>.
                  It sends the mint its hash <code>H</code> and an amount{" "}
                  <code>A</code>. The mint creates a HODL invoice: a Lightning
                  payment that can be accepted and held without being collected.
                </p>
                <Equation>
                  {
                    "H = SHA-256(P)\nh = SHA-256(domain ‖ uint64_be(A) ‖ H) mod r"
                  }
                </Equation>
                <p>
                  Here <code>domain</code> is the literal UTF-8 string{" "}
                  <code>Cashu_PS_HTLC_Claim_v3</code>, <code>H</code> is its raw
                  32-byte hash, and <code>r</code> is the BLS12-381 scalar field
                  order. The resulting attribute <code>h</code> binds every
                  token to both the payment hash and the <em>original</em> held
                  amount.
                </p>
                <p>
                  Once the full payment is held, the mint issues tokens totaling{" "}
                  <code>A</code>. It knows <code>A</code>, <code>H</code>, and{" "}
                  <code>h</code>, but has not received <code>P</code>. Under the
                  payment’s hashlock, it cannot settle that hold without the
                  preimage.
                </p>
                <aside className="crypto-aside">
                  Two different secrets do different jobs: <code>P</code>{" "}
                  unlocks the backing payment; a token’s owner secret{" "}
                  <code>s</code> authorizes spending that credential.
                </aside>
              </section>

              <section id="issuance" aria-labelledby="issuance-title">
                <SectionTitle index={1} />
                <p>
                  Each token carries a Pointcheval–Sanders (PS) signature over
                  two attributes: the backing <code>h</code> and an owner secret{" "}
                  <code>s</code>. The mint has independent signing keys for each
                  binary denomination: 1, 2, 4, 8 sats, and so on.
                </p>
                <Equation>
                  {"σ = (u, v)\nv = (x"}
                  <sub>d</sub>
                  {" + y"}
                  <sub>h,d</sub>
                  {" · h + y"}
                  <sub>s,d</sub>
                  {" · s) · u"}
                </Equation>
                <p>
                  The subscript <code>d</code> selects the denomination’s key.
                  Changing a token’s stated value selects a different key and
                  fails verification. Splitting a 1,000 sat hold into smaller
                  tokens changes their denominations, while every fragment keeps
                  the <code>h</code> derived from the original 1,000 sats.
                </p>
                <ol>
                  <li>
                    The wallet picks a fresh <code>s</code> for each output and
                    sends <code>S = s · G₁</code> with a proof that it knows{" "}
                    <code>s</code>.
                  </li>
                  <li>
                    The mint checks the proof and funded quote, chooses fresh{" "}
                    <code>k</code>, and returns <code>u = k · G₁</code> and the
                    signature point <code>v</code>.
                  </li>
                  <li>
                    The wallet checks the signature locally using the mint’s
                    public keys before saving the credential.
                  </li>
                </ol>
                <aside className="crypto-aside">
                  Initial issuance hides the owner secret, not the backing. The
                  mint already knows which held payment funded the quote. Hiding{" "}
                  <code>h</code> matters when the tokens are swapped later.
                </aside>
                <details className="crypto-details">
                  <summary>The public signature check</summary>
                  <p>
                    <code>G₁</code> and <code>G₂</code> are generators of the
                    two BLS12-381 groups. The mint publishes{" "}
                    <code>
                      X₂ = x<sub>d</sub> · G₂
                    </code>
                    ,{" "}
                    <code>
                      Yₕ,₂ = y<sub>h,d</sub> · G₂
                    </code>
                    , and{" "}
                    <code>
                      Yₛ,₂ = y<sub>s,d</sub> · G₂
                    </code>
                    . With <code>e</code> denoting the pairing:
                  </p>
                  <Equation>
                    {"e(v, G₂) = e(u, X₂ + h · Yₕ,₂) · e(s · u, Yₛ,₂)"}
                  </Equation>
                  <p>
                    Authenticity can be checked offline. Whether the token is
                    unspent and its backing is live still requires the mint.
                  </p>
                </details>
              </section>

              <section id="ownership" aria-labelledby="ownership-title">
                <SectionTitle index={2} />
                <p>
                  A token is a bearer credential. Passing its encoded data
                  passes the secrets needed to use it, so a receiver refreshes
                  it into fresh secrets. Copying a token does not create more
                  spendable value: every spend exposes a deterministic
                  nullifier.
                </p>
                <Equation>
                  {"N = s · G_null\n(u′, v′) = (ρ · u, ρ · v)"}
                </Equation>
                <p>
                  Random <code>ρ</code> changes the signature’s appearance. A
                  Chaum–Pedersen proof shows that the same secret <code>s</code>{" "}
                  underlies the nullifier <code>N</code> and the point{" "}
                  <code>s · u′</code>, without revealing <code>s</code>. The
                  nullifier stays the same even if that credential is presented
                  again with new randomness.
                </p>
                <p>
                  The mint checks the signature and proof, then atomically marks
                  the input nullifiers spent when issuing swap outputs. A second
                  attempt to spend them is rejected. Proofs are bound to the
                  specific request so they cannot authorize different outputs.
                </p>
                <aside className="crypto-aside">
                  Until the receiver refreshes, both sender and receiver may
                  hold the same bearer secrets. The first successful spend wins.
                </aside>
                <h3>The backing preimage must travel too.</h3>
                <p>
                  A normal wallet send includes the backing preimage{" "}
                  <code>P</code>
                  alongside the credentials, payment hash <code>H</code>, and
                  original backing amount <code>A</code>. The sender passes this
                  bundle privately to the recipient. Every later holder needs{" "}
                  <code>P</code> to redeem to Lightning without asking the
                  original funder for help.
                </p>
                <p>
                  The recipient checks <code>SHA-256(P) = H</code> locally.
                  Receiving and private swaps do not send <code>P</code> to the
                  mint; a Lightning melt must reveal it for every backing used.
                  Swaps retire old credentials without revealing that preimage.
                  Refreshing rotates the owner secret <code>s</code>, while
                  <code> P</code> stays the same across all fragments of one
                  hold. Knowing <code>P</code> alone does not authorize spending
                  someone else’s credential: its owner proof is still required.
                </p>
                <aside className="crypto-aside">
                  The current token format permits a missing preimage. The
                  browser can receive and refresh that credential, but cannot
                  melt it or use the normal Send flow until the matching
                  preimage is added. For a payment you can redeem independently,
                  require the preimage as part of receipt.
                </aside>
                <h3>How the receiver checks the backing and value</h3>
                <ol>
                  <li>
                    <strong>Identify the mint.</strong> The wallet checks the
                    bundle’s mint URL against the connected mint and verifies
                    credentials with that mint’s public denomination keys and
                    matching keyset identifier. A sender’s choice of mint does
                    not make it trustworthy.
                  </li>
                  <li>
                    <strong>Bind the metadata to the credential.</strong> Check
                    the supplied preimage against <code>H</code> and recompute
                    <code> h</code> from <code>H</code> and the original amount
                    <code> A</code> using the formula above. It must equal the
                    credential’s signed attribute. This prevents substituting a
                    different payment hash or backing amount while retaining a
                    valid signature.
                  </li>
                  <li>
                    <strong>Verify and total the denominations.</strong> For
                    each fragment, verify the PS signature with the key for its
                    claimed denomination <code>d</code>. Changing that amount
                    selects the wrong key and fails verification. Reject
                    duplicate nullifiers, then sum the verified denominations
                    and compare that total with the payment you expected. The
                    original backing amount <code>A</code> is not the amount
                    being sent.
                  </li>
                  <li>
                    <strong>Check the live backing.</strong> Ask the mint about
                    each <code>H</code> through{" "}
                    <code>POST /v1/htlcs/check</code>. Its returned held amount
                    must match <code>A</code>, and the redemption cutoff must
                    still be in the future. A missing or unavailable backing
                    does not pass the check. This sends the payment hash, not
                    the preimage, to the mint.
                  </li>
                  <li>
                    <strong>Check and refresh ownership.</strong> Query the
                    input nullifiers through <code>POST /v1/checkstate</code>{" "}
                    and require <code>UNSPENT</code>. A status check alone
                    cannot prevent a competing spend. The receiver then swaps
                    into fresh owner secrets; the mint consumes the old
                    nullifiers atomically, and the wallet verifies the returned
                    credentials and denominations. The backing metadata and
                    preimage stay with the refreshed tokens.
                  </li>
                </ol>
                <Equation>
                  {
                    "original backing A = 1,000 sats\nreceived fragments = 8 sats + 2 sats\nreceived payment = 10 sats, not 1,000 sats"
                  }
                </Equation>
                <p>
                  These checks establish authentic claims for the stated value
                  and a mint-reported live backing. They do not independently
                  prove that Lightning funds remain locked. The mint can
                  misreport its backend, backing can close after a check, and a
                  previous holder can reveal the shared preimage. Receipt
                  therefore still depends on the mint honoring redemption and
                  its settlement policy.
                </p>
              </section>

              <section id="swaps" aria-labelledby="swaps-title">
                <SectionTitle index={3} />
                <p>
                  A private swap consumes credentials and issues fresh ones with
                  equal total value. It can split, combine, or refresh tokens,
                  provided they all share one backing. The mint sees the
                  denominations and input nullifiers, but <code>h</code> stays
                  hidden.
                </p>
                <SwapExample />
                <p>
                  Each input uses a randomized presentation with a fresh blinder{" "}
                  <code>oᵢ</code>. Each output commits to the same backing with
                  its own fresh blinder <code>tⱼ</code>. All public keys below
                  are selected for the corresponding denomination.
                </p>
                <Equation>
                  {
                    "INPUT i     κᵢ = h · Yₕ,₂[dᵢ] + oᵢ · G₂\nOUTPUT j    Bⱼ = h · Yₕ,₁[dⱼ] + tⱼ · G₁\n\nΣ input denominations = Σ output denominations"
                  }
                </Equation>
                <p>
                  The input signature is adjusted to match the hidden backing:
                  <code> v″ = ρ · v + oᵢ · u′</code>. One common-witness
                  Schnorr-style proof establishes that the <em>same</em>{" "}
                  <code>h</code> appears in every input and output equation.
                  This prevents moving value onto another payment hash without
                  revealing the backing.
                </p>
                <details className="crypto-details">
                  <summary>How blind reissuance preserves that backing</summary>
                  <p>
                    For each output the wallet also sends a fresh owner
                    commitment <code>Sⱼ = sⱼ · G₁</code> and its knowledge
                    proof. The mint chooses fresh signing randomness{" "}
                    <code>kⱼ</code>:
                  </p>
                  <Equation>
                    {"uⱼ = kⱼ · G₁\nv_raw,j = kⱼ · (x"}
                    <sub>d</sub>
                    {" · G₁ + Bⱼ + y"}
                    <sub>s,d</sub>
                    {" · Sⱼ)\nvⱼ = v_raw,j − tⱼ · uⱼ"}
                  </Equation>
                  <p>
                    Removing the blinding gives a PS signature on the original
                    <code> h</code> and new <code>sⱼ</code>. The swap transcript
                    binds the ordered input amounts and nullifiers, output
                    commitments, and owner proofs to this request.
                  </p>
                </details>
                <p>
                  A single private swap cannot combine different backing holds.
                  The wallet refreshes mixed bundles in separate swaps. Because
                  the server cannot identify the backing inside a swap, wallets
                  check its status separately. A fresh signature never extends
                  an expired hold.
                </p>
              </section>

              <section id="redemption" aria-labelledby="redemption-title">
                <SectionTitle index={4} />
                <p>
                  Paying a BOLT11 invoice is a <em>melt</em>. Unlike a private
                  swap, it can combine tokens from different holds. It reveals
                  each backing preimage <code>P</code> and original amount, and
                  proves ownership with the backing attribute public. The mint
                  hashes each supplied preimage, recomputes the backing
                  attribute, and checks it against the signed credential. A
                  credential without its matching preimage cannot be melted.
                </p>
                <Equation>
                  {
                    "ecash consumed = invoice sats + ⌈actual fee_msat / 1,000⌉\nchange = input sats − ecash consumed"
                  }
                </Equation>
                <p>
                  The proofs bind the exact quote, invoice, ordered inputs, and
                  change commitments. The mint reserves input nullifiers before
                  paying from its own outgoing liquidity. The receiver’s invoice
                  has its own payment hash, independent of the backing holds.
                  Unused fee reserve and excess input value return as tokens
                  tied to their original backings while those holds remain live.
                </p>
                <p>
                  A partial melt normally leaves its backing pending. The mint’s
                  policy is to settle a known preimage when the full original
                  amount has been consumed, including fees, or at the redemption
                  cutoff. Uncertain payout outcomes keep inputs reserved;
                  retries replay the saved request instead of starting a second
                  payment.
                </p>
                <aside className="crypto-aside crypto-aside-warning">
                  Revealing <code>P</code> gives the mint the technical ability
                  to collect the <em>whole</em> backing payment immediately.
                  Waiting after a partial melt is mint policy, not a
                  cryptographic lock.
                </aside>
              </section>

              <section id="trust" aria-labelledby="trust-title">
                <SectionTitle index={5} />
                <h3>The claim ends with the hold.</h3>
                <p>
                  Settling, canceling, or timing out the backing ends every
                  remaining token’s claim. Redeem before the wallet’s published
                  cutoff. For LND it accounts for the earliest accepted HTLC
                  expiry, the node’s early cancellation delta, and a settlement
                  margin. Spark uses the funded contract’s reported expiry minus
                  a margin. These deadlines are separate from the invoice’s
                  payment window.
                </p>
                <p>
                  If no preimage has been revealed, the mint cannot settle and
                  the held payment eventually returns to its original payer. If
                  the mint settles after a partial redemption, it collects the
                  full hold and keeps any unredeemed remainder. There is no
                  continuing user balance to claim. Sending, refreshing, or
                  backing up tokens does not reset their deadline.
                </p>
                <h3>Private proofs do not hide every observation.</h3>
                <p>
                  The mint sees each swap’s amounts, inputs, outputs, timing,
                  and network connection. The proof hides its backing and does
                  not say whether ownership changed. Separate backing-status
                  requests explicitly disclose the payment hashes being checked;
                  checking several together can associate them with one wallet.
                  Redemption also identifies the backings used. Amounts, timing,
                  and these lookups can help correlate activity.
                </p>
                <h3>The mint still has to honor redemption.</h3>
                <p>
                  Signatures prevent holders from inventing value or changing
                  backing under the scheme’s assumptions. They do not force a
                  mint to pay, remain online, or follow its delayed-settlement
                  policy. Backing checks are mint/backend reports, not
                  independent proof of locked funds. This is an experimental
                  protocol, and its <code>cashuPS</code> tokens are incompatible
                  with ordinary Cashu wallets.
                </p>
                <div className="crypto-source-links">
                  <span className="eyebrow">CHECK THE IMPLEMENTATION</span>
                  <a href="https://github.com/a1denvalu3/tmp.cash/blob/main/tmp_cash/crypto/ps.py">
                    Signatures and proofs{" "}
                    <ArrowUpRight size={14} aria-hidden="true" />
                  </a>
                  <a href="https://github.com/a1denvalu3/tmp.cash/blob/main/tmp_cash/mint.py">
                    Mint lifecycle <ArrowUpRight size={14} aria-hidden="true" />
                  </a>
                  <a href="https://github.com/a1denvalu3/tmp.cash/blob/main/web-wallet/packages/cashu-ts/src/tmp-cash/client.ts">
                    Wallet verification{" "}
                    <ArrowUpRight size={14} aria-hidden="true" />
                  </a>
                  <a href="https://github.com/a1denvalu3/tmp.cash/blob/main/web-wallet/packages/coco/tmp-cash/Manager.ts">
                    Receive flow <ArrowUpRight size={14} aria-hidden="true" />
                  </a>
                  <a href="https://github.com/a1denvalu3/tmp.cash#readme">
                    Protocol documentation{" "}
                    <ArrowUpRight size={14} aria-hidden="true" />
                  </a>
                </div>
              </section>
            </article>
          </div>
          <section className="crypto-next" aria-labelledby="crypto-next-title">
            <div>
              <span className="eyebrow">FROM PROOFS TO PAYMENTS</span>
              <h2 id="crypto-next-title">Try temporary cash.</h2>
              <p>Connect to a mint you trust. Keep an eye on the deadline.</p>
            </div>
            <div className="crypto-next-actions">
              <WalletLink />
              <a className="text-button" href="/#how-it-works">
                <ArrowLeft size={15} aria-hidden="true" /> Back to the basics
              </a>
            </div>
          </section>
        </main>
        <PublicPageFooter />
      </div>
    </div>
  );
}
