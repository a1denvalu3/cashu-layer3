import { useState } from "react";
import {
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  Box,
  Check,
  Clock3,
  Code2,
  Fingerprint,
  Layers3,
  LockKeyhole,
  Moon,
  RotateCcw,
  Sun,
  Zap,
} from "lucide-react";
import { useTheme } from "./theme";
import "./landing.css";

const format = (amount: number) => amount.toLocaleString("en-US");

function WalletLink() {
  return (
    <a className="button dark landing-open" href="/wallet/">
      Open wallet <ArrowUpRight size={17} aria-hidden="true" />
    </a>
  );
}

function CashIllustration() {
  return (
    <figure
      className="landing-art"
      aria-label="Ecash can split into smaller tokens while keeping the same held Lightning backing."
    >
      <div className="art-backdrop" aria-hidden="true">
        <div className="art-grid" />
        <div className="art-orbit" />
      </div>
      <div className="art-held">
        <span className="art-lightning">
          <Zap size={16} aria-hidden="true" />
        </span>
        1,000 sat backing <span className="art-held-status">HELD</span>
      </div>
      <div className="art-connection" aria-hidden="true" />
      <div className="art-note">
        <div className="art-note-top">
          <span>
            cashu<sup>³</sup>
          </span>
          <Box size={25} strokeWidth={1.3} aria-hidden="true" />
        </div>
        <div className="art-note-value">
          750<span>sats</span>
        </div>
        <div className="art-note-bottom">
          <span>YOURS TO PASS ON</span>
          <Layers3 size={18} aria-hidden="true" />
        </div>
        <div className="art-note-seal" aria-hidden="true">
          <Box size={67} strokeWidth={0.7} />
        </div>
      </div>
      <div className="art-fragment">
        <div>
          <span>FRESH POCKET. SAME BACKING.</span>
          <ArrowUpRight size={17} aria-hidden="true" />
        </div>
        <strong>
          250 <span>sats</span>
        </strong>
        <span className="art-fragment-rule" />
        <span>
          <Fingerprint size={14} aria-hidden="true" /> Privately transferred
        </span>
      </div>
      <figcaption>
        <Clock3 size={14} aria-hidden="true" /> One backing. One original
        deadline.
      </figcaption>
    </figure>
  );
}

function SatsExample() {
  const [shared, setShared] = useState(250);
  const [phase, setPhase] = useState<"shared" | "paid" | "closed">("shared");
  const remaining = 1000 - shared;
  const closed = phase === "closed";

  return (
    <section className="sats-example" aria-labelledby="example-title">
      <div className="example-intro">
        <span className="eyebrow">LET’S FOLLOW THE SATS</span>
        <h2 id="example-title">
          One hold.
          <br />
          Two pockets.
        </h2>
        <p>
          You fund 1,000 sats, then share some with a friend. Move the slider
          and see what happens when they spend.
        </p>
        <div className="example-backing">
          <LockKeyhole size={20} aria-hidden="true" />
          <div>
            <strong>1,000 sats</strong>
            <span>
              {closed ? "Backing settled" : "Original Lightning backing held"}
            </span>
          </div>
          <span className={`example-indicator ${closed ? "closed" : ""}`} />
        </div>
        <p className="example-disclaimer">
          An illustrative example. No real funds move.
        </p>
      </div>
      <div className="example-playground">
        <div
          className="example-progress"
          role="group"
          aria-label={`Example: ${phase === "shared" ? "tokens shared" : phase === "paid" ? "friend paid an invoice" : "backing settled"}`}
        >
          <span className="active">01 Share</span>
          <ArrowRight size={13} aria-hidden="true" />
          <span className={phase !== "shared" ? "active" : ""}>02 Spend</span>
          <ArrowRight size={13} aria-hidden="true" />
          <span className={closed ? "active" : ""}>03 Cutoff</span>
        </div>
        <label className="example-slider-label" htmlFor="share-amount">
          Send to a friend <strong>{format(shared)} sats</strong>
        </label>
        <input
          id="share-amount"
          className="example-slider"
          type="range"
          min="50"
          max="950"
          step="50"
          value={shared}
          aria-valuetext={`${format(shared)} sats`}
          disabled={phase !== "shared"}
          onChange={(event) => setShared(Number(event.target.value))}
          aria-describedby="example-explanation"
        />
        <div className="example-pockets">
          <div className={`example-pocket ${closed ? "expired" : ""}`}>
            <span>Your pocket</span>
            <strong>
              {closed ? "0" : format(remaining)} <span>sats</span>
            </strong>
            <span>
              {closed
                ? `${format(remaining)} sats expired`
                : "Same backing. Same deadline."}
            </span>
          </div>
          <div className="example-pocket friend">
            <span>Your friend’s pocket</span>
            <strong>
              {phase === "shared" ? format(shared) : "0"} <span>sats</span>
            </strong>
            <span>
              {phase === "shared"
                ? "Fresh owner secrets."
                : `${format(shared - 1)} paid + 1 sat fee`}
            </span>
          </div>
        </div>
        <div
          className="example-explanation"
          id="example-explanation"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {phase === "shared" ? (
            <>
              <Layers3 size={19} aria-hidden="true" />
              <p>
                The tokens have changed hands. The original 1,000 sat Lightning
                payment is still held.
              </p>
            </>
          ) : phase === "paid" ? (
            <>
              <Zap size={19} aria-hidden="true" />
              <p>
                Your friend’s payment revealed the backing preimage. Your{" "}
                {format(remaining)} sats remain usable while the mint keeps the
                hold open.
              </p>
            </>
          ) : (
            <>
              <Clock3 size={19} aria-hidden="true" />
              <p>
                At the cutoff, the mint collects the full hold using the
                revealed preimage. Your remaining {format(remaining)} sats
                expire. There is no balance to claim afterward.
              </p>
            </>
          )}
        </div>
        <div className="example-actions">
          {phase === "shared" ? (
            <button className="button dark" onClick={() => setPhase("paid")}>
              Pay a {format(shared - 1)} sat invoice{" "}
              <ArrowRight size={16} aria-hidden="true" />
            </button>
          ) : phase === "paid" ? (
            <button className="button dark" onClick={() => setPhase("closed")}>
              Reach the redemption cutoff{" "}
              <ArrowRight size={16} aria-hidden="true" />
            </button>
          ) : (
            <span className="example-complete">
              <Check size={16} aria-hidden="true" /> Backing closed. Claims
              ended.
            </span>
          )}
          {phase !== "shared" && (
            <button
              className="text-button example-reset"
              onClick={() => setPhase("shared")}
            >
              <RotateCcw size={14} aria-hidden="true" /> Start again
            </button>
          )}
        </div>
        <p className="example-fee">
          Example routing fee: 1 sat. In your wallet, the mint quotes the fee
          reserve before you pay.
        </p>
      </div>
    </section>
  );
}

const steps = [
  {
    icon: LockKeyhole,
    title: "Hold a little Lightning.",
    text: "Pay a funding invoice. The payment stays held in Lightning, and the mint issues ecash against it. Your Lightning wallet may show the payment as pending.",
  },
  {
    icon: Layers3,
    title: "Split it. Pass it on.",
    text: "Send smaller amounts from that backing. Receiving privately swaps the sender’s proofs for fresh ones you control, keeping the original backing and deadline.",
  },
  {
    icon: Zap,
    title: "Take it back to Lightning.",
    text: "Pay a Lightning invoice with your tokens. A payment can combine several backings, and unused fee reserve comes back as ecash while its backing remains active.",
  },
  {
    icon: Clock3,
    title: "Keep an eye on the clock.",
    text: "Tokens stop being redeemable at their published cutoff, or earlier if the backing closes. Sending, receiving, and backing up never extend that deadline.",
  },
];

export function LandingPage() {
  const { theme, changePreference } = useTheme();
  return (
    <div className="landing">
      <a className="landing-skip" href="#main">
        Skip to content
      </a>
      <div className="landing-shell">
        <header className="header landing-header">
          <a className="brand" href="/" aria-label="cashu3 (cashu cubed) home">
            <span className="brand-mark">
              <Box size={25} strokeWidth={1.6} aria-hidden="true" />
            </span>
            cashu<span>³</span>
          </a>
          <nav aria-label="Main navigation">
            <a href="#how-it-works">How it works</a>
            <a href="#know-the-rules">Know the rules</a>
          </nav>
          <div className="header-tools">
            <button
              className="icon-button theme-toggle"
              aria-label={
                theme === "dark" ? "Use light mode" : "Use night mode"
              }
              onClick={() =>
                changePreference(theme === "dark" ? "light" : "dark")
              }
            >
              {theme === "dark" ? (
                <Sun size={19} aria-hidden="true" />
              ) : (
                <Moon size={19} aria-hidden="true" />
              )}
            </button>
            <WalletLink />
          </div>
        </header>
        <main id="main">
          <section className="landing-hero" aria-labelledby="hero-title">
            <div className="hero-copy">
              <span className="eyebrow hero-eyebrow">
                <span /> A DIFFERENT KIND OF ECASH
              </span>
              <h1 id="hero-title">
                Lightning in.
                <br />
                <span>Cash around.</span>
                <br />
                Lightning out.
              </h1>
              <p>
                Turn a held Lightning payment into ecash you can split, share,
                and spend. It travels freely. Its original backing and deadline
                travel with it.
              </p>
              <div className="hero-actions">
                <WalletLink />
                <a className="text-button" href="#how-it-works">
                  Follow the sats <ArrowDown size={16} aria-hidden="true" />
                </a>
              </div>
              <div className="hero-deadline">
                <Clock3 size={15} aria-hidden="true" />
                <span>Spend before the backing settles or expires.</span>
              </div>
            </div>
            <CashIllustration />
          </section>
          <div className="landing-principles" aria-label="The essentials">
            <span>
              <LockKeyhole size={16} aria-hidden="true" /> Backed by a held
              payment
            </span>
            <span>
              <Fingerprint size={17} aria-hidden="true" /> Private when passed
              on
            </span>
            <span>
              <Clock3 size={16} aria-hidden="true" /> A deadline that stays with
              it
            </span>
          </div>
          <section
            className="landing-how"
            id="how-it-works"
            aria-labelledby="how-title"
          >
            <div className="landing-section-heading">
              <div>
                <span className="eyebrow">01 / HOW IT WORKS</span>
                <h2 id="how-title">
                  Keep the payment held.
                  <br />
                  Let the cash move.
                </h2>
              </div>
              <p>
                From one Lightning hold to many little tokens.
                <br />
                Here’s the journey, start to finish.
              </p>
            </div>
            <div className="landing-steps">
              {steps.map(({ icon: Icon, title, text }, index) => (
                <article className="landing-step" key={title}>
                  <div className="step-top">
                    <span className="mono">0{index + 1}</span>
                    <Icon size={23} strokeWidth={1.4} aria-hidden="true" />
                  </div>
                  <h3>{title}</h3>
                  <p>{text}</p>
                </article>
              ))}
            </div>
          </section>
          <SatsExample />
          <section
            className="landing-rules"
            id="know-the-rules"
            aria-labelledby="rules-title"
          >
            <div className="rules-intro">
              <span className="eyebrow">02 / KNOW WHAT YOU HOLD</span>
              <h2 id="rules-title">
                Good cash.
                <br />
                Clear rules.
              </h2>
              <p>
                A different design comes with different tradeoffs. Understand
                these before adding sats.
              </p>
              <span className="rules-tag">
                <Box size={15} aria-hidden="true" /> EXPERIMENTAL BY DESIGN
              </span>
            </div>
            <div className="rules-list">
              <article>
                <span className="mono">01</span>
                <div>
                  <h3>The deadline really is the deadline.</h3>
                  <p>
                    Settlement, cancellation, or timeout ends the backing. At
                    the published redemption cutoff, remaining tokens become
                    unusable. There is no continuing account balance or claim
                    afterward.
                  </p>
                </div>
              </article>
              <article>
                <span className="mono">02</span>
                <div>
                  <h3>A held payment still needs a trusted mint.</h3>
                  <p>
                    The mint controls issuance and payouts. Redeeming reveals a
                    backing’s full preimage, giving the mint the ability to
                    collect that entire hold early. Waiting until the cutoff is
                    mint policy.
                  </p>
                </div>
              </article>
              <article>
                <span className="mono">03</span>
                <div>
                  <h3>Private transfers. Visible redemption.</h3>
                  <p>
                    Swaps hide which Lightning payment backs the tokens, while
                    amounts remain visible to the mint. Paying a Lightning
                    invoice reveals the backing preimages used in that payment.
                  </p>
                </div>
              </article>
              <article>
                <span className="mono">04</span>
                <div>
                  <h3>Your tokens live in this browser.</h3>
                  <p>
                    Keep an encrypted backup of your proofs, preimages, and
                    pending operations. Clearing browser data can lose your
                    wallet. These experimental cashuPS tokens are incompatible
                    with standard Cashu wallets.
                  </p>
                </div>
              </article>
            </div>
          </section>
          <section className="landing-ready" aria-labelledby="ready-title">
            <div>
              <span className="eyebrow">A LITTLE CASH. A LITTLE FREEDOM.</span>
              <h2 id="ready-title">
                Make some room
                <br />
                in your pocket.
              </h2>
              <p>No account to create. Your wallet lives on your device.</p>
            </div>
            <WalletLink />
            <Box
              className="ready-cube"
              size={230}
              strokeWidth={0.45}
              aria-hidden="true"
            />
          </section>
        </main>
        <footer className="landing-footer">
          <span>
            <span className="status-dot" /> cashu³ · EXPERIMENTAL HTLC ECASH
          </span>
          <span>Hold it. Share it. Spend it in time.</span>
          <a
            href="https://github.com/a1denvalu3/cashu-layer3"
            className="text-button"
          >
            <Code2 size={15} aria-hidden="true" /> View the source{" "}
            <ArrowUpRight size={13} aria-hidden="true" />
          </a>
        </footer>
      </div>
    </div>
  );
}
