import { useEffect, useState } from "react";
import {
  ArrowDown,
  ArrowUpRight,
  Clock3,
  Fingerprint,
  Layers3,
  Pause,
  Play,
  RotateCcw,
  ChevronDown,
  UnlockKeyhole,
  LockKeyhole,
  Zap,
} from "lucide-react";
import { TmpCashMark } from "./TmpCashMark";
import {
  PublicPageFooter,
  PublicPageHeader,
  WalletLink,
} from "./PublicPageChrome";
import "./landing.css";

function CashIllustration() {
  return (
    <figure
      className="landing-art"
      aria-label="A frozen Lightning deposit supports ecash that can move privately between people."
    >
      <div className="art-scene">
        <div className="art-backdrop" aria-hidden="true">
          <div className="art-grid" />
          <div className="art-orbit" />
        </div>
        <div className="art-held">
          <span className="art-lightning">
            <Zap size={16} aria-hidden="true" />
          </span>
          1,000 sat deposit <span className="art-held-status">FROZEN</span>
        </div>
        <div className="art-connection" aria-hidden="true" />
        <div className="art-note">
          <div className="art-note-top">
            <span>
              tmp<span>.cash</span>
            </span>
            <TmpCashMark size={25} strokeWidth={1.3} aria-hidden="true" />
          </div>
          <div className="art-note-value">
            750<span>sats</span>
          </div>
          <div className="art-note-bottom">
            <span>ECASH YOU CAN SEND</span>
            <Layers3 size={18} aria-hidden="true" />
          </div>
          <div className="art-note-seal" aria-hidden="true">
            <TmpCashMark size={67} strokeWidth={0.7} />
          </div>
        </div>
        <div className="art-fragment">
          <div>
            <span>SENT TO A FRIEND</span>
            <ArrowUpRight size={17} aria-hidden="true" />
          </div>
          <strong>
            250 <span>sats</span>
          </strong>
          <span className="art-fragment-rule" />
          <span>
            <Fingerprint size={14} aria-hidden="true" /> Owner hidden from mint
          </span>
        </div>
      </div>
      <figcaption>
        <LockKeyhole size={14} aria-hidden="true" /> Deposit frozen. Transfers
        private.
      </figcaption>
    </figure>
  );
}

const diagramSteps = ["Deposit", "Transfer", "Redeem"];
const explanations = [
  "Alice has the ecash. Her Lightning deposit is frozen. The mint cannot collect it.",
  "Alice keeps 750 sats and sends 250 sats to Carol. The mint sees a request to refresh ecash, but cannot tell that ownership changed from Alice to Carol. The deposit stays frozen.",
  "Carol redeems and burns her 250 sats of ecash. The mint can now collect the whole deposit and pay Carol in Lightning. Alice’s 750 sats remain usable only while the mint keeps the deposit open.",
  "If neither redeems in time, Alice’s 750 sats and Carol’s 250 sats of ecash expire and can no longer be spent. When the Lightning hold times out, the 1,000 sat deposit reverts to Alice. The mint never collected it.",
];

function TransferDiagram() {
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [expiredAfterTransfer, setExpiredAfterTransfer] = useState(false);
  const redeemed = step === 2;
  const expired = step === 3;
  const transferred = expired ? expiredAfterTransfer : step > 0;
  const explanation =
    expired && !transferred
      ? "Alice’s 1,000 sats of ecash expire and can no longer be spent. When the Lightning hold times out, the 1,000 sat deposit reverts to Alice. The mint never collected it."
      : explanations[step];

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => {
      if (step < 2) setStep(step + 1);
      else setPlaying(false);
    }, 4500);
    return () => window.clearTimeout(timer);
  }, [playing, step]);

  return (
    <section className="sats-example" aria-labelledby="example-title">
      <div className="example-intro">
        <span className="eyebrow">INTERACTIVE DIAGRAM</span>
        <h2 id="example-title">
          Alice, the mint,
          <br />
          and Carol
        </h2>
        <p>
          Follow a deposit, a transfer, and a redemption. The diagram shows the
          whole story. The mint only sees part of it.
        </p>
        <div
          className="example-mint-view"
          role="group"
          aria-label="What the mint sees"
        >
          <span className="eyebrow">WHAT THE MINT SEES</span>
          <div className="example-backing">
            {expired ? (
              <RotateCcw size={21} aria-hidden="true" />
            ) : redeemed ? (
              <UnlockKeyhole size={21} aria-hidden="true" />
            ) : (
              <LockKeyhole size={21} aria-hidden="true" />
            )}
            <div>
              <strong>1,000 sat deposit</strong>
              <span>
                {expired
                  ? "Reverted to sender"
                  : redeemed
                    ? "Unlocked by redemption"
                    : "Frozen in Lightning"}
              </span>
            </div>
          </div>
          <p>
            {expired
              ? "The mint never collected this deposit."
              : redeemed
                ? "The mint can now collect the deposit."
                : "The mint cannot collect the deposit."}
          </p>
          {step > 0 && (
            <span className="example-request">
              {expired
                ? "Lightning hold timed out"
                : redeemed
                  ? "Request: redeem ecash"
                  : "Request: refresh ecash"}
            </span>
          )}
        </div>
        <div className="example-hidden">
          <Fingerprint size={17} aria-hidden="true" />
          <p>
            <strong>Hidden from the mint</strong>Who passed the ecash to whom.
          </p>
        </div>
        <p className="example-disclaimer">
          Illustration only. No real funds move.
        </p>
      </div>
      <div className="example-playground">
        <div
          className="diagram-controls"
          role="group"
          aria-label="Diagram steps"
        >
          {diagramSteps.map((label, index) => (
            <button
              key={label}
              aria-pressed={step === index}
              onClick={() => {
                setPlaying(false);
                setStep(index);
              }}
            >
              <span>0{index + 1}</span>
              {label}
            </button>
          ))}
        </div>
        <div
          className={`transfer-diagram diagram-step-${step}`}
          data-playing={playing}
          data-transferred={transferred}
          role="group"
          aria-label="Alice, mint, and Carol"
        >
          <svg
            className="diagram-lines"
            viewBox="0 0 1000 900"
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <path className="diagram-deposit-line" d="M220 475 L450 150" />
            <path className="diagram-mint-line" d="M780 475 L550 150" />
            <path
              className="diagram-private-line"
              d="M180 635 V765 H820 V635"
            />
          </svg>
          <div className="diagram-node diagram-mint">
            <span className="diagram-avatar">
              <TmpCashMark size={22} strokeWidth={1.5} aria-hidden="true" />
            </span>
            <strong>Mint</strong>
          </div>
          <div className="diagram-lock">
            {expired ? (
              <RotateCcw size={17} aria-hidden="true" />
            ) : redeemed ? (
              <UnlockKeyhole size={17} aria-hidden="true" />
            ) : (
              <LockKeyhole size={17} aria-hidden="true" />
            )}
            <span>
              {expired ? "Reverted" : redeemed ? "Unlocked" : "Frozen"}
            </span>
          </div>
          {(step === 1 || redeemed) && (
            <div className="diagram-request">
              <Zap size={15} aria-hidden="true" />
              <span>{redeemed ? "Redeem" : "Refresh"}</span>
            </div>
          )}
          <div
            className={`diagram-node diagram-alice ${!expired ? "has-ecash" : ""}`}
            role="group"
            aria-label="Alice"
          >
            <span className="diagram-avatar">A</span>
            <strong>Alice</strong>
            <span>{expired ? "Deposit returned" : "Has ecash"}</span>
          </div>
          <div
            className={`diagram-node diagram-carol ${step === 1 ? "has-ecash" : ""}`}
            role="group"
            aria-label="Carol"
          >
            <span className="diagram-avatar">C</span>
            <strong>Carol</strong>
            <span>
              {step === 1
                ? "Has the ecash"
                : redeemed
                  ? "Gets Lightning"
                  : expired && transferred
                    ? "Ecash expired"
                    : ""}
            </span>
          </div>
          {transferred && (
            <div
              className="diagram-cash diagram-alice-cash"
              role="group"
              aria-label="Alice’s ecash"
            >
              {expired ? (
                <Clock3 size={17} aria-hidden="true" />
              ) : (
                <Layers3 size={17} aria-hidden="true" />
              )}
              <strong>750 sats</strong>
              <span>{expired ? "Expired" : "Ecash"}</span>
            </div>
          )}
          <div
            className="diagram-cash diagram-moving-cash"
            role="group"
            aria-label={transferred ? "Carol’s ecash" : "Alice’s ecash"}
          >
            {expired ? (
              <Clock3 size={17} aria-hidden="true" />
            ) : (
              <Layers3 size={17} aria-hidden="true" />
            )}
            <strong>{transferred ? "250 sats" : "1,000 sats"}</strong>
            <span>{expired ? "Expired" : redeemed ? "Burned" : "Ecash"}</span>
          </div>
        </div>
        <div
          className="example-explanation"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <p>{explanation}</p>
        </div>
        <div className="diagram-actions">
          <button
            className="text-button diagram-play"
            aria-pressed={playing}
            onClick={() => {
              if (playing) setPlaying(false);
              else {
                setStep(0);
                setPlaying(true);
              }
            }}
          >
            {playing ? (
              <Pause size={15} aria-hidden="true" />
            ) : (
              <Play size={15} aria-hidden="true" />
            )}
            {playing ? "Pause explanation" : "Play explanation"}
          </button>
          <button
            className="text-button diagram-expire"
            aria-pressed={expired}
            onClick={() => {
              setPlaying(false);
              if (!expired) setExpiredAfterTransfer(transferred);
              setStep(3);
            }}
          >
            <Clock3 size={15} aria-hidden="true" />{" "}
            {redeemed ? "Instead, miss the deadline" : "Miss the deadline"}
          </button>
        </div>
      </div>
    </section>
  );
}

const concepts = [
  {
    icon: LockKeyhole,
    title: "The mint cannot collect your deposit.",
    text: "Your Lightning payment stays frozen. The mint issues ecash, but cannot collect or spend that deposit until someone redeems ecash from it.",
  },
  {
    icon: Fingerprint,
    title: "Ecash transfers are private.",
    text: "Send ecash to a friend. They can send it to someone else. The mint cannot follow these changes of ownership. The deposit stays frozen while the ecash moves.",
  },
];

const questions = [
  {
    title: "What changes when someone redeems?",
    text: "Redeeming burns the ecash and reveals the secret that unlocks its Lightning deposit. From then on, the mint can collect the whole deposit, even if other people still hold ecash from it. Keeping a partly used deposit frozen until the deadline is mint policy; you must trust the mint to do so.",
  },
  {
    title: "Can the mint see anything during a transfer?",
    text: "The mint sees amounts and requests to refresh ecash. Those requests do not tell it whether ownership changed or who passed ecash to whom. Redeeming to Lightning does reveal which deposit is being used.",
  },
  {
    title: "Does the ecash expire?",
    text: "Yes. Spend it before the deadline shown in your wallet. If its deposit closes or its redemption deadline passes, any remaining ecash stops working. Sending it on or making a backup does not extend that deadline. If no one has redeemed, the Lightning deposit returns to its original payer when the hold times out.",
  },
  {
    title: "What do I need to use it?",
    text: "Connect to a tmp.cash mint you trust to issue ecash and honor redemptions. Your wallet lives in this browser, so keep an encrypted backup before clearing browser data. This is experimental software, and its tokens are incompatible with standard Cashu wallets.",
  },
];

export function LandingPage() {
  return (
    <div className="landing">
      <a className="landing-skip" href="#main">
        Skip to content
      </a>
      <div className="landing-shell">
        <PublicPageHeader />
        <main id="main">
          <section className="landing-hero" aria-labelledby="hero-title">
            <div className="hero-copy">
              <span className="eyebrow hero-eyebrow">
                <span /> tmp.cash / EXPLAINED
              </span>
              <h1 id="hero-title">
                Digital cash,
                <br />
                <span>No custodian</span>
              </h1>
              <p>
                You make a Lightning deposit and receive ecash. The deposit
                stays frozen: the mint cannot collect it until someone redeems
                ecash from it.
              </p>
              <p>
                Ecash is digital cash you can send to other people. The mint
                cannot follow changes of ownership.
              </p>
              <div className="hero-actions">
                <WalletLink />
                <a className="text-button" href="#how-it-works">
                  How it works <ArrowDown size={16} aria-hidden="true" />
                </a>
              </div>
              <div className="hero-deadline">
                <Clock3 size={15} aria-hidden="true" />
                <span>Ecash expires. Redeem before its deadline.</span>
              </div>
            </div>
            <CashIllustration />
          </section>
          <div className="landing-principles" aria-label="The essentials">
            <span>
              <LockKeyhole size={16} aria-hidden="true" /> Deposit frozen until
              redemption
            </span>
            <span>
              <Fingerprint size={17} aria-hidden="true" /> Ownership changes
              stay private
            </span>
          </div>
          <section
            className="landing-how"
            id="how-it-works"
            aria-labelledby="how-title"
          >
            <div className="landing-section-heading">
              <div>
                <span className="eyebrow">01 / THE DEPOSIT AND THE ECASH</span>
                <h2 id="how-title">How it works</h2>
              </div>
            </div>
            <div className="landing-steps">
              {concepts.map(({ icon: Icon, title, text }, index) => (
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
            <aside className="expiry-note" aria-labelledby="expiry-title">
              <Clock3 size={24} strokeWidth={1.5} aria-hidden="true" />
              <div>
                <h3 id="expiry-title">Ecash has a deadline.</h3>
                <p>
                  Redeem before the deadline shown in your wallet. After that,
                  or if the deposit closes earlier, any unspent ecash stops
                  working. There is no remaining balance to claim.
                </p>
                <p>Sending ecash to someone else does not reset the clock.</p>
              </div>
            </aside>
          </section>
          <TransferDiagram />
          <a
            className="landing-crypto-link"
            href="/how-it-works/?view=cryptography"
          >
            <span className="crypto-link-icon">
              <Fingerprint size={26} strokeWidth={1.4} aria-hidden="true" />
            </span>
            <span>
              <strong>How does the cryptography work?</strong>
              <span>
                Follow the signatures, private swaps, and Lightning redemption.
              </span>
            </span>
            <ArrowUpRight size={22} aria-hidden="true" />
          </a>
          <section
            className="landing-rules"
            id="know-the-rules"
            aria-labelledby="rules-title"
          >
            <div className="rules-intro">
              <span className="eyebrow">02 / QUESTIONS</span>
              <h2 id="rules-title">More details</h2>
              <p>Redemption, privacy, and expiry.</p>
            </div>
            <div className="rules-list">
              {questions.map(({ title, text }) => (
                <details key={title}>
                  <summary>
                    {title}
                    <ChevronDown size={17} aria-hidden="true" />
                  </summary>
                  <p>{text}</p>
                </details>
              ))}
            </div>
          </section>
          <section className="landing-ready" aria-labelledby="ready-title">
            <div>
              <span className="eyebrow">BROWSER WALLET</span>
              <h2 id="ready-title">Open the wallet</h2>
              <p>Connect to a compatible mint to get started.</p>
            </div>
            <WalletLink />
            <TmpCashMark
              className="ready-hourglass"
              size={230}
              strokeWidth={0.45}
              aria-hidden="true"
            />
          </section>
        </main>
        <PublicPageFooter />
      </div>
    </div>
  );
}
