import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Copy,
  Download,
  Box,
  Layers3,
  LoaderCircle,
  Monitor,
  Moon,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sun,
  Upload,
  X,
  Zap,
} from "lucide-react";
import QRCode from "qrcode";
import {
  Layer3Manager,
  type Layer3Activity,
  type Layer3Funding,
  type Layer3View,
} from "coco-cashu-core/layer3";
import { Layer3IndexedDbRepository } from "coco-cashu-indexeddb/layer3";
import {
  decodePSToken,
  normalizePSMintUrl,
  psTokenAmount,
  checkPSBacking,
  isPSPendingMeltError,
  type PSMeltQuote,
} from "@cashu/cashu-ts/layer3";
import { decryptBackup, encryptBackup } from "./backup";
import { useTheme } from "./theme";

const repo = new Layer3IndexedDbRepository();
const defaultMint =
  import.meta.env.VITE_DEFAULT_MINT || "http://127.0.0.1:3338";
const initialMint = (() => {
  let saved: string | null = null;
  try {
    saved = localStorage.getItem("layer3-mint");
  } catch {
    /* A blocked storage preference must not prevent the wallet from opening. */
  }
  for (const candidate of [saved, defaultMint, "http://127.0.0.1:3338"]) {
    if (!candidate) continue;
    try {
      return normalizePSMintUrl(candidate);
    } catch {
      /* Fall back from a malformed saved preference or build configuration. */
    }
  }
  return "http://127.0.0.1:3338";
})();
const format = (n: number) => new Intl.NumberFormat("en-US").format(n);
const short = (hash: string) => hash.slice(0, 8) + "…" + hash.slice(-6);
type Dialog =
  | "fund"
  | "send"
  | "receive"
  | "pay"
  | "backup"
  | "restore"
  | "token"
  | "preimage"
  | null;
const purposeNames = {
  mint: "Added ecash",
  send: "Sent ecash",
  receive: "Received ecash",
  melt: "Lightning payment",
};

function deadlineLabel(
  timestamp: number | null,
  blocks: number | null,
): string {
  if (timestamp !== null) {
    const seconds = Math.max(0, timestamp - Math.floor(Date.now() / 1000));
    if (seconds === 0) return "Deadline passed";
    if (seconds < 60) return `${seconds}s left`;
    if (seconds < 3600)
      return `${Math.floor(seconds / 60)}m ${seconds % 60}s left`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m left`;
  }
  return blocks === null ? "Deadline unavailable" : `${blocks} blocks left`;
}
function Modal({
  title,
  subtitle,
  children,
  close,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  close: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current!;
    d.showModal();
    return () => d.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="modal"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(event) => {
        if (event.target === ref.current) close();
      }}
    >
      <button
        className="icon-button modal-close"
        aria-label="Close dialog"
        onClick={close}
      >
        <X size={20} />
      </button>
      <span className="eyebrow">LAYER3 / WALLET</span>
      <h2>{title}</h2>
      {subtitle && <p className="muted">{subtitle}</p>}
      {children}
    </dialog>
  );
}
function QR({ value }: { value: string }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    let active = true;
    setSrc("");
    QRCode.toDataURL(value, {
      width: 260,
      margin: 2,
      errorCorrectionLevel: "L",
      color: { dark: "#232822", light: "#ffffff" },
    })
      .then((url) => {
        if (active) setSrc(url);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [value]);
  return src ? (
    <img className="qr" src={src} alt="Scan to use this invoice or token" />
  ) : (
    <p className="muted small">Use the copy button to share this payload.</p>
  );
}
function CopyButton({
  value,
  label = "Copy",
}: {
  value: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false),
    [error, setError] = useState("");
  return (
    <>
      <button
        type="button"
        className="button secondary"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
          } catch {
            setError("Select and copy the text below.");
          }
        }}
      >
        {copied ? <Check size={16} /> : <Copy size={16} />}
        {copied ? "Copied" : label}
      </button>
      {error && (
        <p role="status" className="small">
          {error}
        </p>
      )}
    </>
  );
}
function ActivityRow({
  row,
  showToken,
}: {
  row: Layer3Activity;
  showToken: (value: string) => void;
}) {
  const incoming = row.purpose === "mint" || row.purpose === "receive";
  return (
    <div className="activity-row">
      <span className={`transaction-icon ${incoming ? "incoming" : ""}`}>
        {row.purpose === "melt" ? (
          <Zap size={19} />
        ) : incoming ? (
          <ArrowDownLeft size={20} />
        ) : (
          <ArrowUpRight size={20} />
        )}
      </span>
      <div className="activity-name">
        <strong>{purposeNames[row.purpose]}</strong>
        <span className="small muted">
          {new Date(row.createdAt).toLocaleString([], {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
          {row.receipt ? ` · ${format(row.receipt.fee)} sat fee` : ""}
        </span>
      </div>
      {row.token && (
        <button
          className="text-button small"
          onClick={() => showToken(row.token!)}
        >
          View token <ChevronRight size={14} />
        </button>
      )}
      <div className="activity-value">
        <strong>
          {incoming ? "+" : "−"}
          {format(row.amount)} <span className="small muted">sats</span>
        </strong>
        <span className="small muted">
          {row.state === "sent" ? "Shared token" : "Completed"}
        </span>
      </div>
    </div>
  );
}

export function App() {
  const { preference, theme, changePreference } = useTheme();
  const [mintUrl, setMintUrl] = useState(initialMint),
    [mintInput, setMintInput] = useState(initialMint);
  const [manager, setManager] = useState<Layer3Manager | null>(null),
    [view, setView] = useState<Layer3View | null>(null);
  const [tab, setTab] = useState<"wallet" | "activity" | "settings">("wallet"),
    [dialog, setDialog] = useState<Dialog>(null);
  const [busy, setBusy] = useState(false),
    [connecting, setConnecting] = useState(true),
    [connectionError, setConnectionError] = useState("");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [amount, setAmount] = useState("");
  const [selectedHash, setSelectedHash] = useState(""),
    [tokenText, setTokenText] = useState(""),
    [invoice, setInvoice] = useState("");
  const [quote, setQuote] = useState<PSMeltQuote | null>(null);
  const [funding, setFunding] = useState<Layer3Funding | null>(null),
    [password, setPassword] = useState(""),
    [backupFile, setBackupFile] = useState<File | null>(null);
  const [tick, setTick] = useState(0),
    [reconnect, setReconnect] = useState(0);
  const busyRef = useRef(false);
  const fundingRef = useRef<Layer3Funding | null>(null);
  useEffect(() => {
    fundingRef.current = funding;
  }, [funding]);

  useEffect(() => {
    let active = true;
    const m = new Layer3Manager(repo, mintUrl);
    const update = async () => {
      const v = await m.view();
      if (active) setView(v);
    };
    const unsubscribe = m.events.on("change", update);
    setConnecting(true);
    setConnectionError("");
    setManager(null);
    (async () => {
      await update();
      try {
        await m.init();
        if (active) {
          setManager(m);
          setConnectionError("");
        }
      } catch (err) {
        if (active)
          setConnectionError(
            err instanceof Error ? err.message : "Mint is unavailable",
          );
      } finally {
        if (active) {
          setConnecting(false);
          await update();
        }
      }
    })();
    const refresh = setInterval(() => {
      if (active && !busyRef.current && m.wallet.keysetId)
        m.refresh()
          .then(() => {
            if (active) {
              setManager(m);
              setConnectionError("");
            }
          })
          .catch((err) => {
            if (active) setConnectionError(err.message);
          });
    }, 15000);
    return () => {
      active = false;
      unsubscribe();
      clearInterval(refresh);
    };
  }, [mintUrl, reconnect]);
  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!manager) return;
    let active = true,
      polling = false;
    const poll = async () => {
      if (polling || busyRef.current) return;
      polling = true;
      try {
        const state = await repo.read();
        for (const f of state.fundings.filter(
          (f) => f.mint === mintUrl && f.state === "waiting",
        )) {
          try {
            const result = await manager.pollFunding(f.id);
            if (active) {
              setFunding((current) =>
                current?.id === result.id ? result : current,
              );
              if (dialog === "fund" && fundingRef.current?.id === result.id)
                setError("");
            }
          } catch (err) {
            // A broken older quote must not stop later deposits being claimed.
            if (active && dialog === "fund" && fundingRef.current?.id === f.id)
              setError(
                err instanceof Error ? err.message : "Funding check failed",
              );
          }
        }
      } catch (err) {
        if (active && dialog === "fund")
          setError(err instanceof Error ? err.message : "Funding check failed");
      } finally {
        polling = false;
      }
    };
    const timer = setInterval(poll, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [manager, mintUrl, dialog]);

  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      return await fn();
    } catch (err) {
      if (isPSPendingMeltError(err)) {
        setDialog(null);
        setNotice(
          "Payment pending. Your request is saved; use Retry to check it safely.",
        );
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong");
      }
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
  }
  function open(next: Dialog) {
    setError("");
    setNotice("");
    setAmount("");
    setTokenText("");
    setInvoice("");
    setQuote(null);
    setSelectedHash("");
    setFunding(null);
    setPassword("");
    setBackupFile(null);
    setDialog(next);
  }
  function close() {
    if (!busy) {
      setDialog(null);
      setError("");
      setPassword("");
    }
  }
  function showToken(value: string) {
    open("token");
    setTokenText(value);
  }
  function switchMint(event: FormEvent) {
    event.preventDefault();
    try {
      const url = normalizePSMintUrl(mintInput);
      localStorage.setItem("layer3-mint", url);
      setMintUrl(url);
      setReconnect((n) => n + 1);
      setNotice("Mint saved. Connecting…");
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid URL");
    }
  }
  const localActivity = (view?.activity ?? [])
    .filter((a) => a.mint === mintUrl)
    .sort((a, b) => b.createdAt - a.createdAt);
  const localOperations = (view?.operations ?? []).filter(
    (a) => a.mint === mintUrl,
  );
  const localFundings = (view?.fundings ?? []).filter(
    (f) => f.mint === mintUrl && f.state === "waiting",
  );
  const groups = Object.values(
    (view?.proofs ?? [])
      .filter((p) => p.mint === mintUrl && p.state === "ready")
      .reduce<
        Record<string, { hash: string; amount: number; redeemable: number }>
      >((acc, p) => {
        const group = acc[p.proof.payment_hash] ?? {
          hash: p.proof.payment_hash,
          amount: 0,
          redeemable: 0,
        };
        group.amount += p.proof.amount;
        if (p.proof.preimage) group.redeemable += p.proof.amount;
        acc[p.proof.payment_hash] = group;
        return acc;
      }, {}),
  );
  const backedGroups = groups.map((g) => ({
    ...g,
    backing: view?.snapshot?.htlcs.find((h) => h.payment_hash === g.hash),
  }));
  const canSpend = !!manager && !!view?.verified;
  const usableBalance = (view?.proofs ?? [])
    .filter((p) => {
      if (
        !view?.verified ||
        !view.snapshot ||
        p.mint !== mintUrl ||
        p.state !== "ready"
      )
        return false;
      try {
        checkPSBacking(p.proof, view.snapshot);
        return true;
      } catch {
        return false;
      }
    })
    .reduce((n, p) => n + p.proof.amount, 0);
  const nearest = backedGroups
    .filter((g) => g.backing)
    .sort(
      (a, b) =>
        (a.backing!.melt_deadline ?? Infinity) -
        (b.backing!.melt_deadline ?? Infinity),
    )[0]?.backing;
  void tick;

  return (
    <div className="app-shell">
      <header className="header">
        <a className="brand" href="/" aria-label="cashu3 (cashu cubed) home">
          <span className="brand-mark">
            <Box size={25} strokeWidth={1.6} />
          </span>
          cashu<span>³</span>
          <sup>WALLET</sup>
        </a>
        <nav aria-label="Main navigation">
          {(["wallet", "activity", "settings"] as const).map((name) => (
            <button
              key={name}
              aria-current={tab === name ? "page" : undefined}
              className={tab === name ? "nav-active" : ""}
              onClick={() => {
                setTab(name);
                setError("");
                setNotice("");
              }}
            >
              {name[0].toUpperCase() + name.slice(1)}
            </button>
          ))}
        </nav>
        <div className="header-tools">
          <span className="local-tag">
            <span className={`status-dot ${canSpend ? "" : "offline"}`} />
            {connecting
              ? "Connecting"
              : canSpend
                ? "Mint connected"
                : "Mint offline"}
          </span>
          <button
            className="icon-button theme-toggle"
            aria-label={theme === "dark" ? "Use light mode" : "Use night mode"}
            title={theme === "dark" ? "Use light mode" : "Use night mode"}
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
        </div>
      </header>
      <main>
        {(connectionError || (error && !dialog)) && (
          <div className="banner error" role="alert">
            <span>
              {connectionError ? `Mint unavailable: ${connectionError}` : error}
            </span>
            {connectionError && (
              <button
                className="text-button"
                onClick={() => setReconnect((n) => n + 1)}
              >
                Reconnect <RefreshCw size={14} />
              </button>
            )}
          </div>
        )}
        {notice && (
          <div className="banner" role="status">
            {notice.startsWith("Payment pending.") ? (
              <Clock3 size={18} />
            ) : (
              <CheckCircle2 size={18} />
            )}
            {notice}
          </div>
        )}
        {tab === "wallet" && (
          <>
            <div className="page-intro">
              <div>
                <span className="eyebrow">YOUR EVERYDAY ECASH</span>
                <h1>Cash, with a deadline.</h1>
              </div>
              <p>
                Hold it. Share it. Spend it.
                <br />
                Your sats stay backed by a live HTLC.
              </p>
            </div>
            <section className="balance-card" aria-label="Wallet balance">
              <div className="balance-main">
                <div className="balance-label">
                  <span className="eyebrow">AVAILABLE TO SPEND</span>
                  <span
                    className="balance-status"
                    title={
                      canSpend
                        ? "Backing status verified with the mint"
                        : "Connect to verify backing status"
                    }
                  >
                    <ShieldCheck size={15} aria-hidden="true" />
                    {canSpend ? "Verified" : "Unverified"}
                  </span>
                </div>
                <div className="balance-number" data-testid="balance">
                  <strong>
                    {view?.verified ? format(usableBalance || 0) : "—"}
                  </strong>
                  <span>sats</span>
                </div>
                <p className="balance-caption">
                  {view?.verified
                    ? `${groups.length} ${groups.length === 1 ? "active backing" : "active backings"} · stored on this device`
                    : "Connect to verify your available ecash"}
                </p>
                <div className="wallet-actions">
                  <button
                    className="button dark"
                    disabled={!manager}
                    onClick={() => open("fund")}
                  >
                    <Plus size={19} />
                    Add sats
                  </button>
                  <button
                    className="button translucent"
                    disabled={!manager}
                    onClick={() => open("receive")}
                  >
                    <ArrowDownLeft size={19} />
                    Receive
                  </button>
                  <button
                    className="button translucent"
                    disabled={!canSpend || !view?.redeemable}
                    onClick={() => open("send")}
                  >
                    <ArrowUpRight size={19} />
                    Send
                  </button>
                </div>
              </div>
              <div className="balance-side">
                <span className="cash-mark" aria-hidden="true">
                  <Layers3 size={83} strokeWidth={0.8} />
                </span>
                <div className="deadline-summary">
                  <Clock3 size={17} />
                  <span>
                    {nearest
                      ? deadlineLabel(
                          nearest.melt_deadline,
                          view?.snapshot?.block_height !== null &&
                            nearest.melt_deadline_height !== null
                            ? Math.max(
                                0,
                                nearest.melt_deadline_height -
                                  (view?.snapshot?.block_height ?? 0),
                              )
                            : null,
                        )
                      : "Every sat has an expiry"}
                  </span>
                </div>
                <span className="small">
                  Spend before the backing deadline.
                </span>
              </div>
            </section>
            <div className="quick-pay">
              <span className="quick-pay-icon">
                <Zap size={24} strokeWidth={1.6} />
              </span>
              <div>
                <strong>Pay any Lightning invoice</strong>
                <p>Redeem ecash, including tokens from different backings.</p>
              </div>
              <button
                className="button secondary"
                disabled={!canSpend || !view?.redeemable}
                onClick={() => open("pay")}
              >
                Pay with Lightning <ArrowRight size={17} />
              </button>
            </div>
            {!!localOperations.length && (
              <section className="pending-panel">
                <div className="section-heading">
                  <h2>Pending operations</h2>
                  <span className="small muted">
                    Requests are saved. Retry safely.
                  </span>
                </div>
                {localOperations.map((o) => (
                  <div className="pending-row" key={o.id}>
                    <LoaderCircle size={18} />
                    <div>
                      <strong>
                        {purposeNames[o.purpose]} · {format(o.amount)} sats
                      </strong>
                      <p>{o.error || "This operation is ready to resume."}</p>
                    </div>
                    <button
                      className="button secondary"
                      disabled={busy || !manager}
                      onClick={() =>
                        run(async () => {
                          const result = await manager!.retry(o.id);
                          if (result.token) showToken(result.token);
                          else setNotice("Operation completed.");
                        })
                      }
                    >
                      Retry
                    </button>
                  </div>
                ))}
              </section>
            )}
            {!!localFundings.length && (
              <section className="fundings-panel">
                {localFundings.map((f) => (
                  <button
                    className="funding-row"
                    key={f.id}
                    onClick={() => {
                      open("fund");
                      setFunding(f);
                    }}
                  >
                    <span className="status-dot waiting" />
                    <span>{format(f.amount)} sats awaiting funding</span>
                    <span className="small muted">
                      Open invoice <ChevronRight size={14} />
                    </span>
                  </button>
                ))}
              </section>
            )}
            <div className="content-grid">
              <section className="backings-section">
                <div className="section-heading">
                  <h2>
                    Your backings <span className="count">{groups.length}</span>
                  </h2>
                  <button
                    className="icon-button"
                    disabled={busy || !manager}
                    aria-label="Refresh backing status"
                    onClick={() => run(() => manager!.refresh())}
                  >
                    <RefreshCw size={16} />
                  </button>
                </div>
                {!!view?.verified && view.expired > 0 && (
                  <p
                    className="small muted"
                    role="status"
                    data-testid="expired-balance"
                  >
                    Expired token records: {format(view.expired)} sats. Their
                    backing settled or reached its deadline; these tokens are no
                    longer spendable. Their records remain stored on this
                    device.
                  </p>
                )}
                {backedGroups.length ? (
                  <div className="backings-list">
                    {backedGroups.map((g) => {
                      const h = g.backing,
                        blocks =
                          h &&
                          view?.snapshot?.block_height !== null &&
                          h.melt_deadline_height !== null
                            ? Math.max(
                                0,
                                h.melt_deadline_height -
                                  (view?.snapshot?.block_height ?? 0),
                              )
                            : null;
                      const urgent = h?.melt_deadline
                        ? h.melt_deadline - Date.now() / 1000 < 300
                        : blocks !== null && blocks < 6;
                      return (
                        <article className="backing" key={g.hash}>
                          <div className="backing-top">
                            <span className="mono small">{short(g.hash)}</span>
                            <span className={`badge ${urgent ? "urgent" : ""}`}>
                              <Clock3 size={12} />
                              {h
                                ? deadlineLabel(h.melt_deadline, blocks)
                                : "Unverified"}
                            </span>
                          </div>
                          <div className="backing-amount">
                            {format(g.amount)} <span>sats</span>
                          </div>
                          <div className="backing-bottom">
                            <span>
                              {g.redeemable === g.amount ? (
                                "Ready to spend"
                              ) : (
                                <button
                                  className="text-button small"
                                  onClick={() => {
                                    open("preimage");
                                    setSelectedHash(g.hash);
                                  }}
                                >
                                  Add backing preimage{" "}
                                  <ChevronRight size={13} />
                                </button>
                              )}
                            </span>
                            <span className="mono small">
                              {h
                                ? `${format(h.amount)} sat backing`
                                : "Checking backing"}
                            </span>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                ) : (
                  <div className="empty-state">
                    <div className="empty-illustration">
                      <Layers3 size={39} strokeWidth={1} />
                      <span />
                    </div>
                    <strong>A little cash. A little freedom.</strong>
                    <p>
                      Add sats or receive a token to get started.
                      <br />
                      Your active backings will appear here.
                    </p>
                    <button
                      className="text-button"
                      disabled={!manager}
                      onClick={() => open("fund")}
                    >
                      Add your first sats <ArrowRight size={16} />
                    </button>
                  </div>
                )}
              </section>
              <aside className="principles-card">
                <span className="eyebrow">BUILT DIFFERENTLY</span>
                <h2>
                  In your pocket.
                  <br />
                  On your terms.
                </h2>
                <div className="principle">
                  <span>01</span>
                  <div>
                    <strong>Backed, while you hold it</strong>
                    <p>
                      Sats remain in a Lightning HTLC until redemption or its
                      deadline.
                    </p>
                  </div>
                </div>
                <div className="principle">
                  <span>02</span>
                  <div>
                    <strong>Fresh tokens when you receive</strong>
                    <p>Private swaps replace the sender’s proofs with yours.</p>
                  </div>
                </div>
                <div className="principle">
                  <span>03</span>
                  <div>
                    <strong>Use it before it expires</strong>
                    <p>
                      Tokens expire when their backing settles or times out.
                      There is no balance to claim afterward.
                    </p>
                  </div>
                </div>
              </aside>
            </div>
            <section className="recent-section">
              <div className="section-heading">
                <h2>Recent activity</h2>
                <button
                  className="text-button small"
                  onClick={() => setTab("activity")}
                >
                  View all <ArrowRight size={15} />
                </button>
              </div>
              {localActivity.slice(0, 3).map((row) => (
                <ActivityRow key={row.id} row={row} showToken={showToken} />
              ))}
              {!localActivity.length && (
                <p className="quiet-empty">
                  Your first transaction starts the story.
                </p>
              )}
            </section>
          </>
        )}
        {tab === "activity" && (
          <>
            <div className="page-intro">
              <div>
                <span className="eyebrow">ON THIS DEVICE</span>
                <h1>The paper trail.</h1>
              </div>
              <p>
                Payments, transfers, and
                <br />
                tokens you have shared.
              </p>
            </div>
            <section className="activity-section">
              {localActivity.length ? (
                localActivity.map((row) => (
                  <ActivityRow key={row.id} row={row} showToken={showToken} />
                ))
              ) : (
                <div className="empty-state">
                  <Clock3 size={36} strokeWidth={1} />
                  <h2>No activity yet.</h2>
                  <p>Your transactions will appear here.</p>
                </div>
              )}
            </section>
          </>
        )}
        {tab === "settings" && (
          <>
            <div className="page-intro">
              <div>
                <span className="eyebrow">MAKE IT YOURS</span>
                <h1>A wallet you control.</h1>
              </div>
              <p>
                Your proofs and preimages
                <br />
                live in this browser.
              </p>
            </div>
            <div className="settings-grid">
              <section className="settings-card appearance-card">
                <h2>Appearance</h2>
                <p className="muted">
                  Choose light or night mode, or follow your device’s setting.
                </p>
                <fieldset className="theme-options">
                  <legend className="sr-only">Color theme</legend>
                  {(
                    [
                      { value: "light", label: "Light", icon: Sun },
                      { value: "dark", label: "Night", icon: Moon },
                      { value: "system", label: "System", icon: Monitor },
                    ] as const
                  ).map(({ value, label, icon: Icon }) => (
                    <label key={value} className="theme-option">
                      <input
                        type="radio"
                        name="theme"
                        value={value}
                        checked={preference === value}
                        onChange={() => changePreference(value)}
                      />
                      <span>
                        <Icon size={18} aria-hidden="true" />
                        {label}
                      </span>
                    </label>
                  ))}
                </fieldset>
              </section>
              <section className="settings-card">
                <h2>Your mint</h2>
                <p className="muted">
                  Connect to a cashu3 mint that supports this wallet.
                </p>
                <form onSubmit={switchMint}>
                  <label htmlFor="mint-url">Mint URL</label>
                  <input
                    id="mint-url"
                    type="url"
                    value={mintInput}
                    onChange={(e) => setMintInput(e.target.value)}
                    required
                    spellCheck={false}
                  />
                  <button className="button dark" type="submit" disabled={busy}>
                    Save and connect <ArrowRight size={16} />
                  </button>
                </form>
                <div className="mint-info mono small">
                  {manager
                    ? `Keyset ${short(manager.wallet.keysetId)}`
                    : "Waiting for a mint connection"}
                </div>
                {manager?.wallet.defaultTokenLifetimeBlocks != null && (
                  <p className="small muted">
                    New tokens:{" "}
                    {manager.wallet.defaultTokenLifetimeBlocks.toLocaleString()}{" "}
                    blocks (about{" "}
                    {(
                      manager.wallet.defaultTokenLifetimeBlocks / 144
                    ).toLocaleString(undefined, {
                      maximumFractionDigits: 1,
                    })}{" "}
                    days). Set by the mint. Each funded HTLC has its own
                    redemption deadline.
                  </p>
                )}
                {manager?.wallet.defaultTokenLifetimeBlocks == null &&
                  manager?.wallet.fundingHtlcCltvDelta != null && (
                    <p className="small muted">
                      Minimum Lightning funding timelock:{" "}
                      {manager.wallet.fundingHtlcCltvDelta} blocks. Each funded
                      backing HTLC determines the token redemption deadline.
                    </p>
                  )}
              </section>
              <section className="settings-card">
                <h2>Keep a copy.</h2>
                <p className="muted">
                  Back up your tokens, preimages, and pending operations in a
                  password-encrypted file. Clearing browser data removes this
                  wallet.
                </p>
                <button className="button dark" onClick={() => open("backup")}>
                  <Download size={17} />
                  Export backup
                </button>
                <button
                  className="button secondary"
                  onClick={() => open("restore")}
                >
                  <Upload size={17} />
                  Restore backup
                </button>
                <p className="small muted">
                  Restore into an empty wallet in a new browser profile. Backups
                  do not extend token deadlines.
                </p>
              </section>
            </div>
          </>
        )}
      </main>
      <footer>
        <span>
          <span className="status-dot" /> YOUR KEYS LIVE HERE
        </span>
        <span>cashu3 · Experimental HTLC ecash</span>
        <a
          className="text-button small wallet-learn-link"
          href="/#how-it-works"
        >
          How cashu³ works <ArrowUpRight size={13} />
        </a>
      </footer>
      {dialog && (
        <Modal
          close={close}
          title={
            {
              fund:
                funding?.state === "minted"
                  ? "Sats in your pocket."
                  : "Add sats.",
              send: "Pass it on.",
              receive: "Receive ecash.",
              pay: quote ? "Ready to pay?" : "Pay with Lightning.",
              backup: "Keep a copy.",
              restore: "Bring your wallet back.",
              token: "Your ecash token.",
              preimage: "Complete this backing.",
            }[dialog]
          }
          subtitle={
            {
              fund: "Fund a hold invoice. Your ecash appears when the payment is held.",
              send: "Create a fresh token to share. Send from one backing at a time.",
              receive:
                "Paste a cashuPS token. We’ll refresh its proofs before adding it.",
              pay: "Redeem tokens from one or several backings.",
              backup: "Protect this file with a password you can remember.",
              restore: "Restore an encrypted backup into this empty wallet.",
              token:
                "Share this token with its recipient before the backing deadline.",
              preimage:
                "Add the preimage from the person who funded this HTLC.",
            }[dialog]
          }
        >
          {error && (
            <div className="banner error" role="alert">
              {error}
            </div>
          )}
          {dialog === "fund" &&
            (!funding ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  run(async () => {
                    setFunding(await manager!.createFunding(Number(amount)));
                  });
                }}
              >
                <label htmlFor="fund-amount">Amount in sats</label>
                <div className="amount-input">
                  <input
                    autoFocus
                    id="fund-amount"
                    type="number"
                    min="1"
                    step="1"
                    required
                    value={amount}
                    onChange={(e) => setAmount(e.target.value)}
                    placeholder="1,000"
                  />
                  <span>sats</span>
                </div>
                <p className="small muted">
                  Pay this invoice within one hour. Funded tokens have a
                  separate redemption deadline set by their backing HTLC.
                </p>
                <button
                  className="button dark full"
                  disabled={busy || !manager}
                >
                  {busy ? (
                    <LoaderCircle className="spinning" size={18} />
                  ) : (
                    <Plus size={18} />
                  )}
                  Create funding invoice
                </button>
              </form>
            ) : funding.state === "minted" ? (
              <div className="success-state">
                <CheckCircle2 size={48} />
                <h3>{format(funding.amount)} sats received</h3>
                <p>Your ecash is ready. Keep an eye on its deadline.</p>
                <button className="button dark full" onClick={close}>
                  Back to wallet
                </button>
              </div>
            ) : funding.state === "closed" ? (
              <div className="success-state">
                <Clock3 size={42} />
                <h3>This invoice has closed.</h3>
                <button
                  className="button dark full"
                  onClick={() => setFunding(null)}
                >
                  Create another invoice
                </button>
              </div>
            ) : (
              <>
                <QR value={funding.quote?.request ?? funding.id} />
                <div className="waiting-status">
                  <span className="status-dot waiting" />
                  {funding.quote
                    ? `Waiting for ${format(funding.amount)} sats`
                    : "Recovering your funding invoice"}
                </div>
                {funding.quote && (
                  <>
                    <CopyButton
                      value={funding.quote.request}
                      label="Copy invoice"
                    />
                    <textarea
                      className="payload"
                      readOnly
                      aria-label="Funding invoice"
                      value={funding.quote.request}
                      rows={3}
                    />
                  </>
                )}
                <button
                  className="button secondary full"
                  disabled={busy || !manager}
                  onClick={() =>
                    run(async () =>
                      setFunding(await manager!.pollFunding(funding.id)),
                    )
                  }
                >
                  <RefreshCw size={16} />
                  Check payment
                </button>
                <p className="small muted">
                  Your Lightning wallet may show “pending” while the backing
                  HTLC remains held.
                </p>
              </>
            ))}
          {dialog === "send" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                run(async () => {
                  const token = await manager!.send(
                    Number(amount),
                    selectedHash || undefined,
                  );
                  showToken(token);
                });
              }}
            >
              <label htmlFor="send-amount">Amount in sats</label>
              <div className="amount-input">
                <input
                  autoFocus
                  id="send-amount"
                  type="number"
                  min="1"
                  step="1"
                  required
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder="10"
                />
                <span>sats</span>
              </div>
              <label htmlFor="backing-select">Backing</label>
              <select
                id="backing-select"
                value={selectedHash}
                onChange={(e) => setSelectedHash(e.target.value)}
              >
                <option value="">Choose automatically</option>
                {backedGroups
                  .filter((g) => g.redeemable > 0)
                  .map((g) => (
                    <option key={g.hash} value={g.hash}>
                      {short(g.hash)} · {format(g.redeemable)} sats
                    </option>
                  ))}
              </select>
              <p className="small muted">
                The token includes its backing preimage. The recipient must
                spend it before the deadline.
              </p>
              <button className="button dark full" disabled={busy}>
                {busy ? (
                  <LoaderCircle className="spinning" size={18} />
                ) : (
                  <ArrowUpRight size={18} />
                )}
                Create token
              </button>
            </form>
          )}
          {dialog === "receive" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                run(async () => {
                  const value = await manager!.receive(tokenText);
                  close();
                  setNotice(`Received ${format(value)} sats.`);
                  setDialog(null);
                });
              }}
            >
              <label htmlFor="receive-token">Ecash token</label>
              <textarea
                autoFocus
                id="receive-token"
                className="token-input"
                spellCheck={false}
                rows={6}
                placeholder="cashuPS…"
                required
                value={tokenText}
                onChange={(e) => setTokenText(e.target.value)}
              />
              <p className="small muted">
                Use tokens from your connected mint: {mintUrl}
              </p>
              <button className="button dark full" disabled={busy || !manager}>
                {busy ? (
                  <LoaderCircle className="spinning" size={18} />
                ) : (
                  <ArrowDownLeft size={18} />
                )}
                Receive token
              </button>
            </form>
          )}
          {dialog === "pay" &&
            (!quote ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  run(async () =>
                    setQuote(
                      await manager!.meltQuote(
                        invoice.trim().replace(/^lightning:/i, ""),
                      ),
                    ),
                  );
                }}
              >
                <label htmlFor="pay-invoice">Lightning invoice</label>
                <textarea
                  autoFocus
                  id="pay-invoice"
                  className="token-input"
                  rows={4}
                  required
                  placeholder="lnbc…"
                  value={invoice}
                  onChange={(e) => setInvoice(e.target.value)}
                  spellCheck={false}
                />
                <p className="small muted">
                  The mint quotes the fee reserve. Any unused reserve comes back
                  as ecash while its backing remains active.
                </p>
                <button className="button dark full" disabled={busy}>
                  {busy ? (
                    <LoaderCircle className="spinning" size={18} />
                  ) : (
                    <Zap size={18} />
                  )}
                  Review payment
                </button>
              </form>
            ) : (
              <>
                <div className="payment-summary">
                  <div>
                    <span>Invoice amount</span>
                    <strong>{format(quote.amount)} sats</strong>
                  </div>
                  <div>
                    <span>Mint fee reserve</span>
                    <strong>up to {format(quote.fee_reserve)} sats</strong>
                  </div>
                  <div className="payment-total">
                    <span>Maximum total</span>
                    <strong>
                      {format(quote.amount + quote.fee_reserve)} sats
                    </strong>
                  </div>
                </div>
                <p className="small muted">
                  This reveals the preimage for each backing used. Tokens on a
                  backing expire when that whole HTLC settles.
                </p>
                <button
                  className="button dark full"
                  disabled={
                    busy ||
                    !view ||
                    view.redeemable < quote.amount + quote.fee_reserve
                  }
                  onClick={() =>
                    run(async () => {
                      const receipt = await manager!.melt(quote);
                      setDialog(null);
                      setNotice(
                        `Paid ${format(receipt.amount)} sats · ${format(receipt.fee)} sat fee${receipt.expired_change ? ` · ${format(receipt.expired_change)} sats of change expired with its backing` : ""}.`,
                      );
                    })
                  }
                >
                  {busy ? (
                    <LoaderCircle className="spinning" size={18} />
                  ) : (
                    <Zap size={18} />
                  )}
                  Pay {format(quote.amount)} sats
                </button>
                {view && view.redeemable < quote.amount + quote.fee_reserve && (
                  <p role="alert" className="small">
                    Not enough redeemable ecash for the invoice and fee reserve.
                  </p>
                )}
                <button
                  className="text-button centered"
                  disabled={busy}
                  onClick={() => setQuote(null)}
                >
                  Edit payment
                </button>
              </>
            ))}
          {dialog === "token" && (
            <>
              <div className="token-total">
                {format(psTokenAmount(decodePSToken(tokenText)))}{" "}
                <span>sats</span>
              </div>
              <QR value={tokenText} />
              <CopyButton value={tokenText} label="Copy token" />
              <textarea
                className="payload"
                aria-label="Outgoing token"
                readOnly
                rows={4}
                value={tokenText}
              />
              <p className="small muted">
                This token is saved in Activity so you can copy it again. Anyone
                with the token can spend it.
              </p>
            </>
          )}
          {dialog === "preimage" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                run(async () => {
                  await manager!.attachPreimage(
                    selectedHash,
                    tokenText.trim().toLowerCase(),
                  );
                  setDialog(null);
                  setNotice("Backing preimage added.");
                });
              }}
            >
              <label htmlFor="backing-preimage">32-byte preimage (hex)</label>
              <input
                autoFocus
                id="backing-preimage"
                spellCheck={false}
                required
                pattern="[0-9a-fA-F]{64}"
                value={tokenText}
                onChange={(e) => setTokenText(e.target.value)}
                placeholder="64 hexadecimal characters"
              />
              <p className="mono small muted">Backing {short(selectedHash)}</p>
              <button className="button dark full" disabled={busy}>
                Save preimage
              </button>
            </form>
          )}
          {(dialog === "backup" || dialog === "restore") && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                run(async () => {
                  if (dialog === "backup") {
                    const text = await encryptBackup(
                        await repo.read(),
                        mintUrl,
                        password,
                      ),
                      url = URL.createObjectURL(
                        new Blob([text], { type: "application/json" }),
                      );
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = `cashu3-backup-${new Date().toISOString().slice(0, 10)}.json`;
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                    setDialog(null);
                    setPassword("");
                    setNotice("Encrypted backup downloaded.");
                  } else {
                    if (!backupFile) throw new Error("Choose a backup file");
                    if (backupFile.size > 16_000_000)
                      throw new Error("Backup is too large");
                    const restored = await decryptBackup(
                      await backupFile.text(),
                      password,
                    );
                    await repo.importState(restored.state);
                    localStorage.setItem("layer3-mint", restored.mint);
                    setMintUrl(restored.mint);
                    setMintInput(restored.mint);
                    setReconnect((n) => n + 1);
                    setDialog(null);
                    setPassword("");
                    setNotice(
                      "Backup restored. Verifying tokens and their backings…",
                    );
                  }
                });
              }}
            >
              {dialog === "restore" && (
                <>
                  <label htmlFor="backup-file">Backup file</label>
                  <input
                    id="backup-file"
                    type="file"
                    accept="application/json,.json"
                    required
                    onChange={(e) => setBackupFile(e.target.files?.[0] ?? null)}
                  />
                </>
              )}
              <label htmlFor="backup-password">Backup password</label>
              <input
                autoFocus={dialog === "backup"}
                id="backup-password"
                type="password"
                minLength={12}
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={
                  dialog === "backup" ? "new-password" : "current-password"
                }
                placeholder="At least 12 characters"
              />
              <button className="button dark full" disabled={busy}>
                {busy ? (
                  <LoaderCircle className="spinning" size={18} />
                ) : dialog === "backup" ? (
                  <Download size={18} />
                ) : (
                  <Upload size={18} />
                )}
                {dialog === "backup"
                  ? "Download encrypted backup"
                  : "Restore wallet"}
              </button>
              <p className="small muted">
                Tokens still expire at their backing deadline, including when
                stored in a backup.
              </p>
            </form>
          )}
        </Modal>
      )}
    </div>
  );
}
