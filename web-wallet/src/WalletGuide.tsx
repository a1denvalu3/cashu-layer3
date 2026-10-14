import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Check,
  Clock3,
  Landmark,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";

const dismissalKey = "layer3-wallet-guide-dismissed-v1";
const slides = [
  {
    topic: "Expiry",
    icon: Clock3,
    title: "Your tokens have a deadline.",
    description:
      "Ordinary ecash is not tied to one pending payment. Here, every token depends on a held Lightning payment. Redeem before its backing deadline: settlement, cancellation, or timeout makes any remaining tokens unusable.",
    takeaway:
      "Sending, receiving, or backing up a token does not extend its life.",
  },
  {
    topic: "Mint custody",
    icon: Landmark,
    title: "Backing stays held. Trust still matters.",
    description:
      "An ordinary ecash mint collects your deposit up front. Here, the funding payment stays held in Lightning when tokens are issued. The mint still controls issuance and payouts. Once redemption reveals the funding secret, the mint can collect the entire backing payment early.",
    takeaway:
      "Choose a mint you trust to honor payouts and the redemption cutoff.",
  },
  {
    topic: "Private transfers",
    icon: ShieldCheck,
    title: "Fresh tokens, the same backing.",
    description:
      "Like ordinary ecash, receiving privately swaps the sender’s tokens for fresh ones you control. The mint sees amounts, but the swap hides which funding payment backs them. Sending or receiving keeps the original backing and deadline.",
    takeaway:
      "Token swaps are private; paying a Lightning invoice reveals the backing used.",
  },
  {
    topic: "Settlement",
    icon: RefreshCw,
    title: "When the backing closes, the claim ends.",
    description:
      "Paying a Lightning invoice spends tokens and can return change. A partial payment normally leaves the backing held. The mint collects the full backing when its value is spent, including fees, or at the cutoff if it knows the funding secret.",
    takeaway:
      "Any tokens left at settlement are forfeited. There is no remaining balance to claim.",
  },
];

export function WalletGuide() {
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(dismissalKey) === "true";
    } catch {
      return false;
    }
  });
  const [index, setIndex] = useState(0);
  const toggleButton = useRef<HTMLButtonElement>(null);
  const focusAfterToggle = useRef(false);
  const slide = slides[index];
  const Icon = slide.icon;

  useEffect(() => {
    if (focusAfterToggle.current) {
      toggleButton.current?.focus();
      focusAfterToggle.current = false;
    }
  }, [dismissed]);

  function setVisibility(visible: boolean) {
    focusAfterToggle.current = true;
    setDismissed(!visible);
    setIndex(0);
    try {
      if (visible) localStorage.removeItem(dismissalKey);
      else localStorage.setItem(dismissalKey, "true");
    } catch {
      // The guide remains usable when browser preferences cannot be saved.
    }
  }

  if (dismissed) {
    return (
      <div className="wallet-guide-reopen">
        <button
          ref={toggleButton}
          className="text-button"
          onClick={() => setVisibility(true)}
        >
          <BookOpen size={16} aria-hidden="true" />
          How cashu³ works
          <ArrowRight size={14} aria-hidden="true" />
        </button>
      </div>
    );
  }

  return (
    <section
      className="wallet-guide"
      aria-label="How cashu3 differs from ordinary ecash"
      aria-roledescription="carousel"
      onKeyDown={(event) => {
        let next = index;
        if (event.key === "ArrowLeft") next = Math.max(0, index - 1);
        else if (event.key === "ArrowRight")
          next = Math.min(slides.length - 1, index + 1);
        else return;
        event.preventDefault();
        setIndex(next);
      }}
    >
      <div className="wallet-guide-header">
        <div>
          <span className="eyebrow">A QUICK GUIDE</span>
          <h2>Ecash, with a few differences.</h2>
          <p>Four things to know before you add sats.</p>
        </div>
        <button
          ref={toggleButton}
          className="icon-button wallet-guide-dismiss"
          aria-label="Dismiss wallet guide"
          onClick={() => setVisibility(false)}
        >
          <X size={19} aria-hidden="true" />
        </button>
      </div>
      <div
        className="wallet-guide-topics"
        role="group"
        aria-label="Choose a guide topic"
      >
        {slides.map((item, topicIndex) => (
          <button
            key={item.topic}
            className={topicIndex === index ? "guide-topic-active" : ""}
            aria-current={topicIndex === index ? "step" : undefined}
            aria-controls="wallet-guide-slide"
            onClick={() => setIndex(topicIndex)}
          >
            <span aria-hidden="true">0{topicIndex + 1}</span>
            {item.topic}
          </button>
        ))}
      </div>
      <div className="wallet-guide-live" aria-live="polite" aria-atomic="true">
        <div
          id="wallet-guide-slide"
          className="wallet-guide-slide"
          role="group"
          aria-roledescription="slide"
          aria-label={`${index + 1} of ${slides.length}: ${slide.topic}`}
        >
          <span className="wallet-guide-icon" aria-hidden="true">
            <Icon size={32} strokeWidth={1.5} />
          </span>
          <div className="wallet-guide-copy">
            <h3>{slide.title}</h3>
            <p>{slide.description}</p>
            <p className="wallet-guide-takeaway">{slide.takeaway}</p>
          </div>
        </div>
      </div>
      <div className="wallet-guide-footer">
        <span className="mono small">
          {index + 1} / {slides.length}
        </span>
        <div className="wallet-guide-controls">
          <button
            className="button secondary"
            aria-label="Previous guide slide"
            disabled={index === 0}
            onClick={() => setIndex(index - 1)}
          >
            <ArrowLeft size={16} aria-hidden="true" />
            Back
          </button>
          {index === slides.length - 1 ? (
            <button
              className="button dark"
              onClick={() => setVisibility(false)}
            >
              Got it <Check size={16} aria-hidden="true" />
            </button>
          ) : (
            <button
              className="button dark"
              aria-label="Next guide slide"
              onClick={() => setIndex(index + 1)}
            >
              Next <ArrowRight size={16} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
